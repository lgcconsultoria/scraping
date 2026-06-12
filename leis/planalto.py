#!/usr/bin/env python3
"""Raspador e repositório de leis do portal planalto.gov.br.

Mantém cópias locais versionadas (por hash SHA-256) dos textos legais usados
como referência nas análises de jurisprudência: verificação de vigência,
gate anti-alucinação (Phase 5) e citações em peças.

Leis suportadas: CPC (13.105/2015), CC (10.406/2002), Lei de Alimentos (5.478/68).

Uso:
    python leis/planalto.py                        # atualiza todas as leis
    python leis/planalto.py CPC                    # atualiza só o CPC
    python leis/planalto.py --mostrar CPC 300      # texto do Art. 300 do CPC
    python leis/planalto.py --listar               # lista leis e estado do cache

Variáveis de ambiente:
    PLANALTO_BASE   base alternativa (padrão: https://www.planalto.gov.br)
    PLANALTO_MIN_INTERVALO  segundos entre requisições (padrão: 2)
"""

import argparse
import dataclasses
import html as html_mod
import json
import logging
import os
import re
import sys
import urllib.robotparser
from datetime import date
from urllib.parse import urlsplit

try:
    import requests
except ImportError:  # pragma: no cover
    sys.exit("Este módulo requer 'requests' (pip install requests).")

from nucleo.cache import carregar_json, salvar_json, sha256_texto
from nucleo.http import ClienteHttp, texto_resposta

# ---------------------------------------------------------------------------
# Configuração do portal
# ---------------------------------------------------------------------------

PLANALTO_BASE = os.environ.get(
    "PLANALTO_BASE", "https://www.planalto.gov.br").rstrip("/")

DADOS_DIR_PADRAO = "leis/dados"

CONTATO = "lgclicitacao@gmail.com"
USER_AGENT = (
    "LGCPesquisaJuridica/1.0 (coleta de textos legais publicos para pesquisa; "
    f"rate limit >= 2s; contato: {CONTATO})"
)
MIN_INTERVALO = float(os.environ.get("PLANALTO_MIN_INTERVALO", "2"))


@dataclasses.dataclass
class LeiMeta:
    nome: str       # nome oficial completo
    numero: str     # "13.105", "10.406", "5.478"
    ano: int
    caminho: str    # caminho relativo em planalto.gov.br


LEIS: dict[str, LeiMeta] = {
    "CPC": LeiMeta(
        nome="Código de Processo Civil",
        numero="13.105",
        ano=2015,
        caminho="/ccivil_03/_ato2015-2018/2015/lei/l13105.htm",
    ),
    "CC": LeiMeta(
        nome="Código Civil",
        numero="10.406",
        ano=2002,
        caminho="/ccivil_03/leis/2002/l10406compilado.htm",
    ),
    "Lei_Alimentos": LeiMeta(
        nome="Lei de Alimentos",
        numero="5.478",
        ano=1968,
        caminho="/ccivil_03/leis/l5478.htm",
    ),
}

# ---------------------------------------------------------------------------
# Regex de parsing
# ---------------------------------------------------------------------------

TAG_RE = re.compile(r"<[^>]+>")
SCRIPT_RE = re.compile(r"<script[^>]*>.*?</script>", re.S | re.I)
STYLE_RE = re.compile(r"<style[^>]*>.*?</style>", re.S | re.I)
BLOCO_RE = re.compile(r"</(?:p|div|li|tr|td|th|h[1-6]|blockquote)>|<br\s*/?>", re.I)

# Início de artigo: "Art. 1º", "Art. 1o", "Art. 1°", "Art. 1A"
# O número pode ter letra maiúscula sufixada (artigos intercalados no CC: "1-A", "1A")
ART_RE = re.compile(
    r"^Art\.\s+(\d+(?:-[A-Z]|[A-Z])?)[ºo°°]?",
    re.MULTILINE | re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Extração de texto
# ---------------------------------------------------------------------------

def _html_para_texto(html: str) -> str:
    """Extrai texto limpo do HTML do planalto preservando estrutura por artigo."""
    html = SCRIPT_RE.sub(" ", html)
    html = STYLE_RE.sub(" ", html)
    # Tenta isolar o div#conteudo — presente na maioria dos pages planalto.
    # Usa regex não-gulosa dentro de um lookahead para evitar ingerir divs irmãos.
    for id_attr in ("conteudo", "content", "main"):
        m = re.search(
            r'<div[^>]+id=["\']' + id_attr + r'["\'][^>]*>',
            html, re.I)
        if m:
            html = html[m.start():]
            break
    # Converte elementos de bloco em quebras de linha antes de remover tags.
    html = BLOCO_RE.sub("\n", html)
    texto = TAG_RE.sub("", html)
    texto = html_mod.unescape(texto)
    linhas = [re.sub(r"[ \t]+", " ", l).strip() for l in texto.splitlines()]
    return "\n".join(l for l in linhas if l)


def extrair_artigos(texto_limpo: str) -> dict[str, str]:
    """Divide o texto da lei em artigos numerados.

    Retorna dict { "1": "Art. 1º ...", "2": "Art. 2º ...", ... }.
    O texto de cada artigo inclui os parágrafos, incisos e alíneas que se
    seguem até o próximo artigo.
    """
    posicoes = [(m.group(1).upper(), m.start()) for m in ART_RE.finditer(texto_limpo)]
    artigos: dict[str, str] = {}
    for i, (num, inicio) in enumerate(posicoes):
        fim = posicoes[i + 1][1] if i + 1 < len(posicoes) else len(texto_limpo)
        artigos[num] = texto_limpo[inicio:fim].strip()
    return artigos


# ---------------------------------------------------------------------------
# I/O do repositório local
# ---------------------------------------------------------------------------

def url_lei(nome: str, base: str = PLANALTO_BASE) -> str:
    return base.rstrip("/") + LEIS[nome].caminho


def carregar_lei(nome: str, diretorio: str = DADOS_DIR_PADRAO) -> dict | None:
    """Carrega dados da lei do cache local; retorna None se não existir."""
    caminho = os.path.join(diretorio, f"{nome}.json")
    return carregar_json(caminho)


def artigo_texto(nome: str, numero: str | int,
                 diretorio: str = DADOS_DIR_PADRAO) -> str:
    """Retorna texto do artigo NNN da lei, ou '' se não encontrado no cache."""
    dados = carregar_lei(nome, diretorio)
    if not dados:
        return ""
    return dados.get("artigos", {}).get(str(numero).upper(), "")


def salvar_lei(dados: dict, diretorio: str = DADOS_DIR_PADRAO) -> bool:
    """Persiste dados da lei; retorna True se houve mudança de conteúdo."""
    os.makedirs(diretorio, exist_ok=True)
    nome = dados["lei"]
    caminho = os.path.join(diretorio, f"{nome}.json")
    existente = carregar_json(caminho)
    if existente and existente.get("hash") == dados["hash"]:
        return False
    salvar_json(caminho, dados)
    # Histórico de versões (apenas metadados; o texto fica no .json principal)
    hist = os.path.join(diretorio, f"{nome}_historico.jsonl")
    with open(hist, "a", encoding="utf-8") as arq:
        arq.write(json.dumps({
            "hash": dados["hash"],
            "atualizado_em": dados["atualizado_em"],
            "num_artigos": len(dados.get("artigos", {})),
            "url": dados["url"],
        }, ensure_ascii=False) + "\n")
    return True


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------

def _verificar_robots(cliente: ClienteHttp, base: str) -> bool:
    """Lê robots.txt do planalto e verifica que /ccivil_03/ é acessível."""
    partes = urlsplit(base)
    url_robots = f"{partes.scheme}://{partes.netloc}/robots.txt"
    try:
        resp = cliente.get(url_robots, timeout=30)
    except requests.HTTPError as exc:
        codigo = exc.response.status_code if exc.response is not None else 0
        if 400 <= codigo < 500:
            logging.info("robots.txt indisponível (HTTP %s); sem restrições.", codigo)
            return True
        logging.warning("robots.txt: HTTP %s; prosseguindo com cautela.", codigo)
        return True
    except requests.RequestException as exc:
        logging.warning("robots.txt inacessível (%s); prosseguindo.", exc)
        return True
    analisador = urllib.robotparser.RobotFileParser()
    analisador.parse(resp.text.splitlines())
    rota = base.rstrip("/") + "/ccivil_03/"
    if not analisador.can_fetch(USER_AGENT, rota):
        logging.error("robots.txt bloqueia %s", rota)
        return False
    logging.info("robots.txt lido: /ccivil_03/ permitida.")
    return True


def buscar_lei(nome: str, cliente: ClienteHttp,
               base: str = PLANALTO_BASE) -> dict:
    """Baixa e parseia a lei; retorna o dict de dados pronto para salvar_lei."""
    meta = LEIS[nome]
    url = url_lei(nome, base)
    logging.info("baixando %s (%s/%s) de %s", nome, meta.numero, meta.ano, url)
    resp = cliente.get(url, timeout=90)
    html = texto_resposta(resp)
    texto = _html_para_texto(html)
    hash_atual = sha256_texto(texto)
    artigos = extrair_artigos(texto)
    logging.info("%s: %d artigos extraídos (hash=%s…)", nome, len(artigos), hash_atual[:8])
    return {
        "lei": nome,
        "nome": meta.nome,
        "numero": meta.numero,
        "ano": meta.ano,
        "url": url,
        "hash": hash_atual,
        "atualizado_em": date.today().isoformat(),
        "vigente": True,
        "artigos": artigos,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Atualiza o repositório local de textos legais do planalto.gov.br.")
    parser.add_argument("leis_alvo", nargs="*", metavar="LEI",
                        help="leis a atualizar (padrão: todas); opções: "
                             + ", ".join(sorted(LEIS)))
    parser.add_argument("--out", default=None, metavar="DIR",
                        help=f"diretório de saída (padrão: {DADOS_DIR_PADRAO})")
    parser.add_argument("--mostrar", nargs=2, metavar=("LEI", "ARTIGO"),
                        help="mostra texto de um artigo do cache local")
    parser.add_argument("--listar", action="store_true",
                        help="lista leis conhecidas e estado do cache")
    args = parser.parse_args(argv)

    diretorio = args.out or DADOS_DIR_PADRAO

    if args.listar:
        for nome, meta in sorted(LEIS.items()):
            dados = carregar_lei(nome, diretorio)
            if dados:
                estado = f"cache {dados['atualizado_em']} | {len(dados.get('artigos',{}))} artigos"
            else:
                estado = "não cacheado"
            print(f"{nome:<20} {meta.nome} (Lei {meta.numero}/{meta.ano}) — {estado}")
        return

    if args.mostrar:
        lei_nome, numero = args.mostrar
        if lei_nome not in LEIS:
            print(f"Lei desconhecida: {lei_nome!r}. Opções: {', '.join(sorted(LEIS))}")
            sys.exit(1)
        texto = artigo_texto(lei_nome, numero, diretorio)
        if texto:
            print(texto)
        else:
            print(f"Art. {numero} não encontrado em {lei_nome} "
                  f"(cache em {os.path.abspath(diretorio)}).")
        return

    nomes = args.leis_alvo or list(LEIS)
    invalidos = [n for n in nomes if n not in LEIS]
    if invalidos:
        parser.error(f"lei(s) desconhecida(s): {', '.join(invalidos)} "
                     f"— opções: {', '.join(sorted(LEIS))}")

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    logging.info("planalto.gov.br | base=%s | leis=%s", PLANALTO_BASE, nomes)

    cliente = ClienteHttp(user_agent=USER_AGENT,
                          min_intervalo=MIN_INTERVALO, max_tentativas=5)

    if not _verificar_robots(cliente, PLANALTO_BASE):
        logging.error("robots.txt proíbe acesso; abortando.")
        sys.exit(2)

    atualizadas = 0
    erros = 0
    for nome in nomes:
        try:
            dados = buscar_lei(nome, cliente, base=PLANALTO_BASE)
            if salvar_lei(dados, diretorio):
                atualizadas += 1
                logging.info("%s salva/atualizada (hash=%s…)", nome, dados["hash"][:8])
            else:
                logging.info("%s: sem alteração de conteúdo.", nome)
        except Exception as exc:
            erros += 1
            logging.error("erro ao baixar %s: %s", nome, exc)

    print(f"\nConcluído: {atualizadas} lei(s) atualizadas, {erros} erro(s). "
          f"Cache em: {os.path.abspath(diretorio)}")


if __name__ == "__main__":
    main()
