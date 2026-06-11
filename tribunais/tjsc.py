"""Adaptador para o portal de jurisprudência do TJSC."""
import html as html_mod
import logging
import os
import re
import urllib.robotparser
from urllib.parse import quote, unquote, urlencode, urljoin, urlsplit

import requests

from nucleo.cache import salvar_binario, salvar_texto
from nucleo.http import ClienteHttp, classificar_resposta, texto_resposta
from tribunais.base import AdaptadorTribunal, InteiroTeor

# ---------------------------------------------------------------------------
# Constantes do portal
# ---------------------------------------------------------------------------

CONTATO_PADRAO = "lgclicitacao@gmail.com"
USER_AGENT_PADRAO = (
    "LGCPesquisaJuridica/1.0 (coleta de jurisprudencia publica para pesquisa "
    f"juridica; rate limit >= 2s; contato: {CONTATO_PADRAO})"
)
BASE_PADRAO = "https://busca.tjsc.jus.br/jurisprudencia"

# ---------------------------------------------------------------------------
# Regex
# ---------------------------------------------------------------------------

ID_RE = re.compile(r"(?:rowid|id)=(\d{30})")
ABRE_INTEGRA_RE = re.compile(r"abreIntegra\(\s*'[^']*'\s*,\s*'([^']+)'\s*,\s*'([^']*)'", re.I)
TOTAL_RE = re.compile(r"([\d.,]+)\s*(?:<[^>]*>\s*)*resultados?\s+encontrados?", re.I)
COMENTARIO_RE = re.compile(r"<!--.*?-->", re.S)
TAG_RE = re.compile(r"<[^>]+>")
HREF_RE = re.compile(r"""(?:href|src)\s*=\s*["']([^"']+)["']""", re.I)
SPLIT_PROCESSO_RE = re.compile(r"Processo\s*:")
NUM_CNJ_RE = re.compile(r"\d{7}-?\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}")
NUM_ANTIGO_RE = re.compile(r"\b\d{4}\.\d{6}-?\d\b")
ROTULO_ALT = (
    r"(?:Processo|Relator(?:a|\s*\(a\))?(?:\s+Designad[oa])?|Origem|"
    r"[OÓ]rg[aã]o\s+Julgador|Julgado\s+em|Classe|Decis[aã]o|Ementa|"
    r"Juiz(?:a)?(?:\s+Prolator(?:a)?)?)"
)

MESES_EN = {"jan": "01", "feb": "02", "mar": "03", "apr": "04", "may": "05",
            "jun": "06", "jul": "07", "aug": "08", "sep": "09", "oct": "10",
            "nov": "11", "dec": "12"}

# ---------------------------------------------------------------------------
# Funções auxiliares puras (sem estado)
# ---------------------------------------------------------------------------

def limpar_fragmento(fragmento):
    """Remove tags, resolve entidades HTML e normaliza espaços."""
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


def normalizar_data(valor):
    """Converte data Java ("Thu Aug 27 00:00:00 GMT-03:00 2020") para dd/mm/aaaa."""
    m = re.search(r"\b(\d{1,2}/\d{1,2}/\d{4})\b", valor)
    if m:
        return m.group(1)
    m = re.match(r"[A-Za-z]{3}\s+([A-Za-z]{3})\s+(\d{1,2})\s+[\d:]+\s+\S+\s+(\d{4})", valor)
    if m and m.group(1).lower() in MESES_EN:
        return f"{int(m.group(2)):02d}/{MESES_EN[m.group(1).lower()]}/{m.group(3)}"
    return valor


def parse_resultados(html_pagina):
    """Retorna (registros, total). Exportado no nível de módulo para retrocompatibilidade."""
    html_pagina = COMENTARIO_RE.sub(" ", html_pagina)
    m_total = TOTAL_RE.search(html_pagina)
    total = int(re.sub(r"\D", "", m_total.group(1))) if m_total else 0
    registros = []
    for bloco in SPLIT_PROCESSO_RE.split(html_pagina)[1:]:
        m_ai = ABRE_INTEGRA_RE.search(bloco)
        if m_ai:
            doc_id, tipo = m_ai.group(1), m_ai.group(2) or "acordao_eproc"
        else:
            m_id = ID_RE.search(bloco)
            doc_id, tipo = (m_id.group(1), "acordao_eproc") if m_id else ("", "")
        texto = limpar_fragmento(bloco)
        registros.append({
            "numero_processo": extrair_numero(texto),
            "relator": campo(texto, r"Relator(?:a|\s*\(a\))?"),
            "orgao_julgador": campo(texto, r"[OÓ]rg[aã]o\s+Julgador"),
            "comarca": campo(texto, r"Origem"),
            "data_julgamento": normalizar_data(campo(texto, r"Julgado\s+em")),
            "classe": campo(texto, r"Classe"),
            "doc_id": doc_id,
            "tipo_doc": tipo,
        })
    return registros, total


def tipo_do_documento(linha):
    """Recupera o tipo (acordao, acordao_5, acordao_eproc…) da URL catalogada."""
    m = re.search(r"[?&](?:tipo|categoria)=([^&]+)", linha.get("url_integra", ""))
    return unquote(m.group(1)) if m else "acordao_eproc"


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


# _classificar e _salvar_binario promovidos a nucleo.http / nucleo.cache.


# ---------------------------------------------------------------------------
# AdaptadorTjsc
# ---------------------------------------------------------------------------

class AdaptadorTjsc(AdaptadorTribunal):
    """Implementa o protocolo do portal busca.tjsc.jus.br/jurisprudencia."""

    def __init__(
        self,
        base=None,
        user_agent=None,
        min_intervalo=2.0,
        max_tentativas=5,
        ps=50,
        max_paginas=400,
    ):
        self.base = (base or os.environ.get("TJSC_BASE", BASE_PADRAO)).rstrip("/")
        self.search = f"{self.base}/buscaajax.do?categoria=acordaos"
        self.ps = ps
        self.max_paginas = max_paginas
        self._user_agent = user_agent or USER_AGENT_PADRAO
        self.cliente = ClienteHttp(
            user_agent=self._user_agent,
            min_intervalo=min_intervalo,
            max_tentativas=max_tentativas,
        )

    def url_integra(self, doc_id, tipo="acordao_eproc"):
        return (f"{self.base}/integra.do"
                f"?rowid={quote(doc_id, safe='')}&tipo={quote(tipo, safe='')}")

    def url_html(self, doc_id, tipo="acordao_eproc"):
        return (f"{self.base}/html.do"
                f"?id={quote(doc_id, safe='')}&categoria={quote(tipo, safe='')}")

    def verificar_robots(self) -> bool:
        partes = urlsplit(self.base)
        url_robots = f"{partes.scheme}://{partes.netloc}/robots.txt"
        try:
            resposta = self.cliente.get(url_robots, timeout=30)
        except requests.HTTPError as exc:
            codigo = exc.response.status_code if exc.response is not None else 0
            if 400 <= codigo < 500:
                logging.info("robots.txt indisponível (HTTP %s); sem restrições publicadas.", codigo)
                return True
            logging.warning("falha ao ler robots.txt (HTTP %s); prosseguindo.", codigo)
            return True
        except requests.RequestException as exc:
            logging.warning("falha ao ler robots.txt (%s); prosseguindo.", exc)
            return True
        analisador = urllib.robotparser.RobotFileParser()
        analisador.parse(resposta.text.splitlines())
        rotas = [f"{self.base}/", self.search,
                 self.url_integra("0" * 30), self.url_html("0" * 30)]
        bloqueadas = [r for r in rotas if not analisador.can_fetch(self._user_agent, r)]
        if bloqueadas:
            logging.error("robots.txt desautoriza as rotas: %s", ", ".join(bloqueadas))
            return False
        logging.info("robots.txt lido: rotas necessárias permitidas.")
        return True

    def estabelecer_sessao(self) -> None:
        self.cliente.get(f"{self.base}/", timeout=30)

    def parsear_resultados(self, html: str) -> "tuple[list[dict], int]":
        return parse_resultados(html)

    def buscar(self, relator, params, *, ps=None):
        ps = ps if ps is not None else self.ps
        pagina = 1
        while pagina <= self.max_paginas:
            corpo = {
                "q": "", "frase": "", "qualquer": "", "excluir": "", "nuProcesso": "",
                "datainicial": "", "datafinal": "", "classe": "", "only_ementa": "",
                "relator": relator, "ps": str(ps), "sort": "dtJulgamento desc",
                "page": str(pagina),
            }
            corpo.update({chave: str(valor) for chave, valor in params.items()})
            # O backend decodifica o corpo como ISO-8859-1: UTF-8 corrompe acentos.
            dados = urlencode(corpo, encoding="latin-1", errors="replace")
            resposta = self.cliente.post(
                self.search, data=dados, timeout=60,
                headers={"Content-Type": "application/x-www-form-urlencoded; charset=ISO-8859-1"})
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

    def baixar_inteiro_teor(self, doc_id, tipo, base_destino) -> InteiroTeor:
        """Tenta PDF/RTF via integra.do; cai para links no visualizador; fallback html.do."""
        url0 = self.url_integra(doc_id, tipo)
        resposta = self.cliente.get(url0, stream=True, timeout=120)
        formato, primeiro, iterador = classificar_resposta(resposta)
        if formato:
            caminho = base_destino + "." + formato
            salvar_binario(caminho, primeiro, iterador)
            resposta.close()
            return InteiroTeor(caminho=caminho, formato=formato, url=url0)
        corpo = primeiro + b"".join(iterador)
        url_visualizador = resposta.url or url0
        codificacao = resposta.encoding or "utf-8"
        resposta.close()
        visualizador = corpo.decode(codificacao, errors="replace")
        for candidata in candidatos_pdf(visualizador, url_visualizador, {url0, url_visualizador}):
            try:
                resp2 = self.cliente.get(candidata, stream=True, timeout=120)
            except requests.RequestException as exc:
                logging.debug("candidato a PDF falhou (%s): %s", candidata, exc)
                continue
            formato2, primeiro2, iterador2 = classificar_resposta(resp2)
            if formato2:
                caminho = base_destino + "." + formato2
                salvar_binario(caminho, primeiro2, iterador2)
                resp2.close()
                return InteiroTeor(caminho=caminho, formato=formato2, url=candidata)
            resp2.close()
        url_h = self.url_html(doc_id, tipo)
        try:
            resp_html = self.cliente.get(url_h, timeout=60)
        except requests.HTTPError:
            url_h = self.url_html(doc_id, "acordaos")
            resp_html = self.cliente.get(url_h, timeout=60)
        caminho = base_destino + ".html"
        salvar_texto(caminho, texto_resposta(resp_html))
        return InteiroTeor(caminho=caminho, formato="html", url=url_h)
