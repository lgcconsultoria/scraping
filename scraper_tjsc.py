#!/usr/bin/env python3
"""Raspador de jurisprudência do TJSC (https://busca.tjsc.jus.br/jurisprudencia/).

Coleta acórdãos de 8 desembargadores das 9ª e 10ª Câmaras de Direito Civil em
dois eixos temáticos (dicionário EIXOS, configurável abaixo), baixa o inteiro
teor de cada decisão (PDF, com fallback para HTML) e mantém um catálogo
incremental em <saida>/index.csv.

Uso:
    python scraper_tjsc.py --dry-run    # busca e cataloga, sem baixar nada
    python scraper_tjsc.py              # busca e baixa PDFs/HTMLs
    python scraper_tjsc.py --out DIR    # muda o diretório de saída

Estrutura de saída:
    decisoes_tjsc/
      9a_camara/<relator_slug>/<NUMERO_PROCESSO>.pdf
      10a_camara/<relator_slug>/<NUMERO_PROCESSO>.pdf
      index.csv                      # catálogo (uma linha por acórdão único)
      _raw_html/<relator>_<eixo>_p<N>.html   # snapshots para auditoria
      run.log

Boas práticas embutidas:
  - checa robots.txt antes de operar (aborta se o acesso for desautorizado);
  - User-Agent identificável com contato;
  - intervalo mínimo de 2 s entre TODAS as requisições (rate limiting global);
  - backoff exponencial em HTTP 429/5xx e erros de rede;
  - idempotência: arquivo já existente em disco não é rebaixado; o index.csv
    é gravado em append (retomável) e reconciliado ao final quando linhas
    antigas mudam de estado (ex.: download feito após um --dry-run);
  - try/except por documento: a falha de um PDF não aborta o lote (fica no
    run.log e é retentada na próxima execução).

Aviso: confira os Termos de Uso do portal antes de operar em escala. Este
script não paraleliza requisições.

Variáveis de ambiente (úteis em testes): TJSC_BASE (base alternativa do
portal) e TJSC_MIN_INTERVALO (segundos entre requisições; padrão 2).
"""

import argparse
import collections
import csv
import html as html_mod
import logging
import os
import re
import sys
import time
import unicodedata
import urllib.robotparser
from urllib.parse import urljoin, urlsplit

try:
    import requests
except ImportError:  # pragma: no cover
    sys.exit("Este script requer a biblioteca 'requests' (pip install requests).")

BASE = os.environ.get("TJSC_BASE", "https://busca.tjsc.jus.br/jurisprudencia").rstrip("/")
SEARCH = f"{BASE}/buscaajax.do?categoria=acordaos"
OUT_PADRAO = "decisoes_tjsc"

CONTATO = "lgclicitacao@gmail.com"
USER_AGENT = (
    "LGCPesquisaJuridica/1.0 (coleta de jurisprudencia publica para pesquisa "
    f"juridica; rate limit >= 2s; contato: {CONTATO})"
)
MIN_INTERVALO = float(os.environ.get("TJSC_MIN_INTERVALO", "2"))
MAX_TENTATIVAS = 5          # tentativas por requisição (com backoff exponencial)
PS = 50                     # resultados por página (10, 20 ou 50)
MAX_PAGINAS = 400           # trava de segurança por consulta

RELATORES = {
    "9a_camara": [
        "Cláudia Lambert de Faria",
        "Gerson Cherem II",
        "Haidée Denise Grin",
        "Flavio Andre Paz de Brum",
    ],
    "10a_camara": [
        "Silvio Dagoberto Orsatto",
        "Jaber Farah",
        "Sérgio Luiz Junkes",
        "Marcelo Pons Meirelles",
    ],
}

# Eixos temáticos: cada item vira uma busca por relator. Campos aceitos pelo
# portal: q, frase, qualquer, excluir, classe, nuProcesso, datainicial,
# datafinal, only_ementa. Mantenha 2-4 termos por busca.
EIXOS = {
    "prova_cognicao": {
        "frase": "tutela de urgência",
        "q": "alimentos prova",
        "classe": "Agravo de Instrumento",
    },
    "binomio_renda": {
        "q": "alimentos possibilidade renda",
        "classe": "Agravo de Instrumento",
    },
}

COLUNAS = [
    "numero_processo", "relator", "camara", "orgao_julgador", "comarca",
    "data_julgamento", "classe", "doc_id", "eixo_origem", "formato",
    "arquivo", "url_integra",
]

ID_RE = re.compile(r"(?:rowid|id)=(\d{30})")
TOTAL_RE = re.compile(r"([\d.,]+)\s+resultados?\s+encontrados?", re.I)
TAG_RE = re.compile(r"<[^>]+>")
HREF_RE = re.compile(r"""(?:href|src)\s*=\s*["']([^"']+)["']""", re.I)
SPLIT_PROCESSO_RE = re.compile(r"Processo\s*:")
NUM_CNJ_RE = re.compile(r"\d{7}-?\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}")
NUM_ANTIGO_RE = re.compile(r"\b\d{4}\.\d{6}-?\d\b")
# Rótulos que delimitam o fim do valor de um campo no texto do resultado.
ROTULO_ALT = (
    r"(?:Processo|Relator(?:a|\s*\(a\))?(?:\s+Designad[oa])?|Origem|"
    r"[OÓ]rg[aã]o\s+Julgador|Julgado\s+em|Classe|Decis[aã]o|Ementa|Juiz(?:a)?)"
)


def slug(texto):
    texto = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    texto = re.sub(r"\s+", "_", texto.strip().lower())
    return re.sub(r"[^a-z0-9_]+", "", texto)


def sanitizar_nome(nome):
    return re.sub(r"[^\w.\-]+", "_", nome).strip("._-")[:150]


def url_integra(doc_id):
    return f"{BASE}/integra.do?rowid={doc_id}&tipo=acordao_eproc"


def url_html(doc_id):
    return f"{BASE}/html.do?id={doc_id}&categoria=acordao_eproc"


class ClienteHttp:
    """Sessão HTTP com rate limit global e backoff exponencial em 429/5xx."""

    def __init__(self):
        self.sessao = requests.Session()
        self.sessao.headers.update({"User-Agent": USER_AGENT})
        self._ultimo = 0.0

    def _respeita_intervalo(self):
        falta = MIN_INTERVALO - (time.monotonic() - self._ultimo)
        if falta > 0:
            time.sleep(falta)

    def requisitar(self, metodo, url, **kw):
        kw.setdefault("timeout", 60)
        ultima_exc = None
        for tentativa in range(1, MAX_TENTATIVAS + 1):
            self._respeita_intervalo()
            try:
                resposta = self.sessao.request(metodo, url, **kw)
            except (requests.ConnectionError, requests.Timeout) as exc:
                self._ultimo = time.monotonic()
                ultima_exc = exc
                if tentativa < MAX_TENTATIVAS:
                    espera = min(60, 2 * 2 ** tentativa)
                    logging.warning("erro de rede (%s/%s) em %s: %s; aguardando %ss",
                                    tentativa, MAX_TENTATIVAS, url, exc, espera)
                    time.sleep(espera)
                continue
            self._ultimo = time.monotonic()
            if resposta.status_code in (429, 500, 502, 503, 504) and tentativa < MAX_TENTATIVAS:
                espera = min(60, 2 * 2 ** tentativa)
                retry_after = resposta.headers.get("Retry-After", "").strip()
                if retry_after.isdigit():
                    espera = max(espera, int(retry_after))
                logging.warning("HTTP %s (%s/%s) em %s; aguardando %ss",
                                resposta.status_code, tentativa, MAX_TENTATIVAS, url, espera)
                resposta.close()
                time.sleep(espera)
                continue
            resposta.raise_for_status()
            return resposta
        raise ultima_exc if ultima_exc else RuntimeError(f"tentativas esgotadas: {url}")

    def get(self, url, **kw):
        return self.requisitar("GET", url, **kw)

    def post(self, url, **kw):
        return self.requisitar("POST", url, **kw)


def robots_permite(cliente):
    """Lê o robots.txt do host e verifica as rotas usadas pelo raspador."""
    partes = urlsplit(BASE)
    url_robots = f"{partes.scheme}://{partes.netloc}/robots.txt"
    try:
        resposta = cliente.get(url_robots, timeout=30)
    except requests.HTTPError as exc:
        codigo = exc.response.status_code if exc.response is not None else 0
        if 400 <= codigo < 500:
            logging.info("robots.txt indisponível (HTTP %s); sem restrições publicadas.", codigo)
            return True
        logging.warning("falha ao ler robots.txt (HTTP %s); prosseguindo com cautela.", codigo)
        return True
    except requests.RequestException as exc:
        logging.warning("falha ao ler robots.txt (%s); prosseguindo com cautela.", exc)
        return True
    analisador = urllib.robotparser.RobotFileParser()
    analisador.parse(resposta.text.splitlines())
    rotas = [f"{BASE}/", SEARCH, url_integra("0" * 30), url_html("0" * 30)]
    bloqueadas = [rota for rota in rotas if not analisador.can_fetch(USER_AGENT, rota)]
    if bloqueadas:
        logging.error("robots.txt desautoriza as rotas: %s", ", ".join(bloqueadas))
        return False
    logging.info("robots.txt lido: rotas necessárias permitidas.")
    return True


def texto_resposta(resposta):
    """Texto da resposta corrigindo charset ausente (portais legados usam latin-1)."""
    if "charset" not in resposta.headers.get("Content-Type", "").lower():
        resposta.encoding = resposta.apparent_encoding or resposta.encoding
    return resposta.text


def limpar_fragmento(fragmento):
    """Remove tags, resolve entidades HTML e normaliza espaços (uma linha por trecho)."""
    texto = html_mod.unescape(TAG_RE.sub("\n", fragmento)).replace("\xa0", " ")
    linhas = (re.sub(r"[ \t]+", " ", linha).strip() for linha in texto.splitlines())
    return "\n".join(linha for linha in linhas if linha)


def campo(texto, rotulo):
    m = re.search(rotulo + r"\s*:\s*(.+?)(?=\s*(?:\n|" + ROTULO_ALT + r"\s*:|$))", texto)
    return m.group(1).strip(" -–") if m else ""


def extrair_numero(texto_limpo):
    inicio = texto_limpo[:400]
    m = NUM_CNJ_RE.search(inicio) or NUM_ANTIGO_RE.search(inicio)
    return m.group(0) if m else ""


def parse_resultados(html_pagina):
    """Retorna (registros, total). O portal repete rótulos textuais por registro."""
    m_total = TOTAL_RE.search(html_pagina)
    total = int(re.sub(r"\D", "", m_total.group(1))) if m_total else 0
    registros = []
    for bloco in SPLIT_PROCESSO_RE.split(html_pagina)[1:]:
        m_id = ID_RE.search(bloco)
        texto = limpar_fragmento(bloco)
        registros.append({
            "numero_processo": extrair_numero(texto),
            "relator": campo(texto, r"Relator(?:a|\s*\(a\))?"),
            "orgao_julgador": campo(texto, r"[OÓ]rg[aã]o\s+Julgador"),  # portal grafa "Orgão"
            "comarca": campo(texto, r"Origem"),
            "data_julgamento": campo(texto, r"Julgado\s+em"),
            "classe": campo(texto, r"Classe"),
            "doc_id": m_id.group(1) if m_id else "",
        })
    return registros, total


def buscar(cliente, relator, params_eixo, ps=PS):
    """Gera (registros, total, pagina, html_bruto) paginando o POST de busca."""
    pagina = 1
    while pagina <= MAX_PAGINAS:
        corpo = {
            "q": "", "frase": "", "qualquer": "", "excluir": "", "nuProcesso": "",
            "datainicial": "", "datafinal": "", "classe": "", "only_ementa": "",
            "relator": relator, "ps": str(ps), "sort": "dtJulgamento desc",
            "page": str(pagina),
        }
        corpo.update({chave: str(valor) for chave, valor in params_eixo.items()})
        resposta = cliente.post(SEARCH, data=corpo, timeout=60)
        html_pagina = texto_resposta(resposta)
        registros, total = parse_resultados(html_pagina)
        yield registros, total, pagina, html_pagina
        if not registros:
            break
        if total and pagina * ps >= total:
            break
        if not total and len(registros) < ps:
            break
        pagina += 1


def _classificar(resposta):
    """Espia o primeiro chunk: PDF por Content-Type OU pelos magic bytes %PDF."""
    iterador = resposta.iter_content(8192)
    primeiro = b""
    for chunk in iterador:
        if chunk:
            primeiro = chunk
            break
    eh_pdf = ("pdf" in resposta.headers.get("Content-Type", "").lower()
              or primeiro.lstrip().startswith(b"%PDF"))
    return eh_pdf, primeiro, iterador


def _salvar_binario(caminho, primeiro, iterador):
    tmp = caminho + ".part"
    try:
        with open(tmp, "wb") as arq:
            arq.write(primeiro)
            for chunk in iterador:
                if chunk:
                    arq.write(chunk)
        os.replace(tmp, caminho)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _salvar_texto(caminho, conteudo):
    tmp = caminho + ".part"
    try:
        with open(tmp, "w", encoding="utf-8") as arq:
            arq.write(conteudo)
        os.replace(tmp, caminho)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def candidatos_pdf(texto_html, url_origem, excluir):
    """Links de <a>/<iframe>/<embed> que podem apontar para o PDF real."""
    urls = []
    for bruto in HREF_RE.findall(texto_html):
        u = html_mod.unescape(bruto.strip())
        low = u.lower()
        if low.startswith(("javascript:", "mailto:", "data:", "#")) or "html.do" in low:
            continue
        if ".pdf" in low or "integra" in low or "eproc" in low:
            absoluto = urljoin(url_origem, u)
            if absoluto not in excluir and absoluto not in urls:
                urls.append(absoluto)
    return urls[:6]


def baixar_documento(cliente, doc_id, base_destino):
    """Baixa o inteiro teor em base_destino(.pdf|.html); retorna (caminho, formato, url).

    1) integra.do com Content-Type/magic de PDF -> salva .pdf direto;
    2) integra.do devolveu visualizador HTML -> segue links candidatos a PDF;
    3) sem PDF localizável -> salva a versão html.do como .html (formato=html).
    """
    url0 = url_integra(doc_id)
    resposta = cliente.get(url0, stream=True, timeout=120)
    eh_pdf, primeiro, iterador = _classificar(resposta)
    if eh_pdf:
        caminho = base_destino + ".pdf"
        _salvar_binario(caminho, primeiro, iterador)
        resposta.close()
        return caminho, "pdf", url0
    corpo = primeiro + b"".join(iterador)
    url_visualizador = resposta.url or url0
    codificacao = resposta.encoding or "utf-8"
    resposta.close()
    visualizador = corpo.decode(codificacao, errors="replace")
    for candidata in candidatos_pdf(visualizador, url_visualizador, {url0, url_visualizador}):
        try:
            resp2 = cliente.get(candidata, stream=True, timeout=120)
        except requests.RequestException as exc:
            logging.debug("candidato a PDF falhou (%s): %s", candidata, exc)
            continue
        eh_pdf2, primeiro2, iterador2 = _classificar(resp2)
        if eh_pdf2:
            caminho = base_destino + ".pdf"
            _salvar_binario(caminho, primeiro2, iterador2)
            resp2.close()
            return caminho, "pdf", candidata
        resp2.close()
    resp_html = cliente.get(url_html(doc_id), timeout=60)
    caminho = base_destino + ".html"
    _salvar_texto(caminho, texto_resposta(resp_html))
    return caminho, "html", url_html(doc_id)


def carregar_index(caminho_csv):
    """Carrega o catálogo existente: (todas_as_linhas, dicionário doc_id -> linha)."""
    linhas, por_id = [], {}
    if not os.path.exists(caminho_csv):
        return linhas, por_id
    with open(caminho_csv, newline="", encoding="utf-8") as arq:
        for crua in csv.DictReader(arq):
            linha = {coluna: (crua.get(coluna) or "").strip() for coluna in COLUNAS}
            linhas.append(linha)
            if linha["doc_id"]:
                por_id.setdefault(linha["doc_id"], linha)
    return linhas, por_id


def regravar_index(caminho_csv, linhas):
    tmp = caminho_csv + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as arq:
        escritor = csv.DictWriter(arq, fieldnames=COLUNAS)
        escritor.writeheader()
        escritor.writerows(linhas)
    os.replace(tmp, caminho_csv)


def reservar_base(camara, relator_consulta, doc_id, numero, ocupados):
    """Caminho relativo (sem extensão) do arquivo, com sufixo em caso de colisão
    de nome (ex.: dois acórdãos do mesmo processo: mérito + embargos)."""
    nome = sanitizar_nome(numero) or doc_id
    base_rel = f"{camara}/{slug(relator_consulta)}/{nome}"
    dono = ocupados.get(base_rel)
    if dono and dono != doc_id:
        base_rel = f"{base_rel}_{doc_id[-8:]}"
    ocupados[base_rel] = doc_id
    return base_rel


def arquivo_existente(saida, base_rel):
    for extensao, formato in ((".pdf", "pdf"), (".html", "html")):
        if os.path.exists(os.path.join(saida, base_rel + extensao)):
            return base_rel + extensao, formato
    return "", ""


def configurar_log(saida):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(os.path.join(saida, "run.log"), encoding="utf-8"),
            logging.StreamHandler(),
        ],
        force=True,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Raspa acórdãos do portal de jurisprudência do TJSC "
                    "(8 relatores das 9ª/10ª Câmaras de Direito Civil, 2 eixos temáticos).")
    parser.add_argument("--dry-run", action="store_true",
                        help="faz as buscas e monta o index.csv sem baixar inteiros teores")
    parser.add_argument("--out", default=OUT_PADRAO,
                        help=f"diretório de saída (padrão: {OUT_PADRAO})")
    args = parser.parse_args(argv)

    saida = args.out
    os.makedirs(os.path.join(saida, "_raw_html"), exist_ok=True)
    configurar_log(saida)
    print(f"Aviso: respeite os Termos de Uso do portal TJSC. Este script lê o robots.txt, "
          f"identifica-se como '{USER_AGENT}' e aplica intervalo mínimo de "
          f"{MIN_INTERVALO:g}s entre requisições, sem paralelismo.")
    logging.info("início | dry_run=%s | saida=%s | base=%s | intervalo>=%ss",
                 args.dry_run, saida, BASE, MIN_INTERVALO)

    cliente = ClienteHttp()
    try:
        if not robots_permite(cliente):
            logging.error("Abortando em respeito ao robots.txt do portal.")
            sys.exit(2)
        cliente.get(f"{BASE}/", timeout=30)  # estabelece cookies de sessão
        logging.info("sessão estabelecida em %s/", BASE)
    except SystemExit:
        raise
    except requests.RequestException as exc:
        logging.error("não foi possível acessar %s: %s", BASE, exc)
        sys.exit(1)

    caminho_csv = os.path.join(saida, "index.csv")
    linhas, por_id = carregar_index(caminho_csv)
    if linhas:
        logging.info("catálogo existente carregado: %s linhas.", len(linhas))
    ocupados = {l["arquivo"].rsplit(".", 1)[0]: l["doc_id"] for l in linhas if l["arquivo"]}
    stats = collections.Counter()
    precisa_regravar = False
    csv_novo = not os.path.exists(caminho_csv) or os.path.getsize(caminho_csv) == 0
    arq_csv = open(caminho_csv, "a", newline="", encoding="utf-8")
    escritor = csv.DictWriter(arq_csv, fieldnames=COLUNAS)
    if csv_novo:
        escritor.writeheader()
        arq_csv.flush()

    def baixar(linha, base_rel):
        nonlocal precisa_regravar
        os.makedirs(os.path.join(saida, os.path.dirname(base_rel)), exist_ok=True)
        try:
            caminho, formato, url_final = baixar_documento(
                cliente, linha["doc_id"], os.path.join(saida, base_rel))
        except Exception as exc:
            stats["falhas_download"] += 1
            logging.error("download %s (processo %s): %s",
                          linha["doc_id"], linha["numero_processo"], exc)
            return False
        linha["arquivo"] = os.path.relpath(caminho, saida).replace(os.sep, "/")
        linha["formato"] = formato
        linha["url_integra"] = url_final
        stats["baixados_" + formato] += 1
        return True

    def processar_registro(camara, relator, eixo, reg):
        nonlocal precisa_regravar
        doc_id = reg["doc_id"]
        if not doc_id:
            stats["sem_id"] += 1
            logging.warning("registro sem doc_id (processo=%r) em %s/%s",
                            reg["numero_processo"], relator, eixo)
            return 0
        existente = por_id.get(doc_id)
        if existente is None:
            linha = {coluna: "" for coluna in COLUNAS}
            linha.update(reg)
            linha["relator"] = reg["relator"] or relator
            linha["camara"] = camara
            linha["eixo_origem"] = eixo
            linha["url_integra"] = url_integra(doc_id)
            base_rel = reservar_base(camara, relator, doc_id, reg["numero_processo"], ocupados)
            arquivo, formato = arquivo_existente(saida, base_rel)
            if arquivo:
                linha["arquivo"], linha["formato"] = arquivo, formato
                stats["pulados"] += 1
            elif not args.dry_run:
                baixar(linha, base_rel)
            escritor.writerow(linha)
            arq_csv.flush()
            linhas.append(linha)
            por_id[doc_id] = linha
            stats["novos"] += 1
            return 1
        stats["reencontrados"] += 1
        if args.dry_run:
            return 0
        arquivo = existente["arquivo"]
        if arquivo and os.path.exists(os.path.join(saida, arquivo)):
            stats["pulados"] += 1
            return 0
        base_rel = (arquivo.rsplit(".", 1)[0] if arquivo else
                    reservar_base(existente["camara"] or camara, relator,
                                  doc_id, existente["numero_processo"], ocupados))
        achado, formato = arquivo_existente(saida, base_rel)
        if achado:
            existente["arquivo"], existente["formato"] = achado, formato
            stats["pulados"] += 1
            precisa_regravar = True
            return 0
        if baixar(existente, base_rel):
            precisa_regravar = True
        return 0

    try:
        for camara, nomes in RELATORES.items():
            for relator in nomes:
                for eixo, params in EIXOS.items():
                    stats["consultas"] += 1
                    novos_consulta = 0
                    total_consulta = 0
                    try:
                        for registros, total, pagina, bruto in buscar(cliente, relator, params):
                            total_consulta = total or total_consulta
                            caminho_raw = os.path.join(
                                saida, "_raw_html", f"{slug(relator)}_{eixo}_p{pagina}.html")
                            _salvar_texto(caminho_raw, bruto)
                            logging.info("%s | %s | página %s | total %s | %s registros",
                                         relator, eixo, pagina, total, len(registros))
                            for reg in registros:
                                novos_consulta += processar_registro(camara, relator, eixo, reg)
                        logging.info("consulta concluída: %s | %s | total=%s | novos=%s",
                                     relator, eixo, total_consulta, novos_consulta)
                    except Exception as exc:
                        stats["falhas_busca"] += 1
                        logging.error("busca %s/%s: %s", relator, eixo, exc)
    except KeyboardInterrupt:
        stats["interrompido"] = 1
        logging.warning("interrompido pelo usuário; salvando estado parcial.")
    finally:
        arq_csv.close()
        if precisa_regravar:
            regravar_index(caminho_csv, linhas)
            logging.info("index.csv regravado para refletir downloads/atualizações.")

    resumo = ("consultas={consultas} | novos={novos} | reencontrados={reencontrados} | "
              "baixados_pdf={baixados_pdf} | baixados_html={baixados_html} | "
              "pulados_existentes={pulados} | falhas_download={falhas_download} | "
              "falhas_busca={falhas_busca} | sem_id={sem_id}").format_map(stats)
    logging.info("FIM | %s", resumo)
    print(f"\nConcluído{' (dry-run: nada foi baixado)' if args.dry_run else ''}. {resumo}")
    print(f"Catálogo: {caminho_csv} ({len(linhas)} documentos) | log: "
          f"{os.path.join(saida, 'run.log')}")
    if stats["interrompido"]:
        sys.exit(130)


if __name__ == "__main__":
    main()
