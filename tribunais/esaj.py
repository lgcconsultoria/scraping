"""Adaptador base para portais eSAJ (CJSG — Consulta de Julgados de 2º Grau).

Utilizado por TJSP, TJMS e demais tribunais que adotam o sistema SAJ/eSAJ.

Interface do portal:
  - Sessão : GET  <base>/cjsg/consulta.do
               → extrai conversationId de <input name="conversationId" value="...">
  - Busca  : GET  <base>/cjsg/resultadoCompleta.do
               params: dados.buscaInteiroTeor, dados.pesquisarPor, dados.nomeRelator,
                       dados.dtJulgamentoInicio, dados.dtJulgamentoFim,
                       dados.cdCamaraIsolada, paginaConsulta, registrosPorPagina,
                       conversationId
  - PDF    : GET  <base>/cjsg/getArquivo.do?cdAcordao=NNN&cdForo=NNN

Codificação de doc_id: "<cdAcordao>:<cdForo>" (separador ":").
"""
import html as html_mod
import logging
import os
import re
import urllib.robotparser
from urllib.parse import urlsplit

import requests

from nucleo.cache import salvar_binario, salvar_texto
from nucleo.http import ClienteHttp, classificar_resposta, texto_resposta
from tribunais.base import AdaptadorTribunal, InteiroTeor

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

CONTATO_PADRAO = "lgclicitacao@gmail.com"
USER_AGENT_PADRAO = (
    "LGCPesquisaJuridica/1.0 (coleta de jurisprudencia publica para pesquisa "
    f"juridica; rate limit >= 2s; contato: {CONTATO_PADRAO})"
)

# ---------------------------------------------------------------------------
# Regex de parsing do HTML eSAJ
# ---------------------------------------------------------------------------

# Total de resultados: "Acórdãos (1.234)" ou "Acordaos (1234)"
TOTAL_RE = re.compile(r"Acórd[aã]os?\s*\(\s*([\d.]+)\s*\)", re.I)
# Linha de resultado: <tr ... id="id-doc-N-acordao" ...>...</tr>
LINHA_RE = re.compile(
    r'<tr[^>]+id="id-doc-\d+-acordao"[^>]*>(.*?)</tr>', re.S | re.I)
CD_ACORDAO_RE = re.compile(r'cdAcordao=(\d+)', re.I)
CD_FORO_RE = re.compile(r'cdForo=(\d+)', re.I)
# Número do processo no bloco (CNJ ou formato antigo)
NUM_CNJ_RE = re.compile(r'\d{7}-?\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}')
NUM_ANTIGO_RE = re.compile(r'\b\d{4}\.\d{6}-?\d\b')
# conversationId na página de consulta
CONV_ID_RE = re.compile(r'name="conversationId"\s+value="([^"]*)"', re.I)
TAG_RE = re.compile(r'<[^>]+>')

# ---------------------------------------------------------------------------
# Funções auxiliares puras
# ---------------------------------------------------------------------------

def _limpar(fragmento):
    """Remove tags e normaliza espaços."""
    return re.sub(r'[ \t]+', ' ',
                  html_mod.unescape(TAG_RE.sub(' ', fragmento)).replace('\xa0', ' ')).strip()


def _campo(bloco_html, rotulo):
    """Extrai valor após <strong>Rótulo(...):</strong> valor<br>."""
    m = re.search(
        r'<strong>[^<]*' + re.escape(rotulo) + r'[^<]*</strong>\s*:?\s*([^\n<]+)',
        bloco_html, re.I)
    return html_mod.unescape(m.group(1).strip()) if m else ""


def _extrair_numero(bloco_html):
    """Extrai número CNJ ou antigo do bloco HTML do resultado."""
    texto = _limpar(bloco_html)[:600]
    m = NUM_CNJ_RE.search(texto) or NUM_ANTIGO_RE.search(texto)
    return m.group(0) if m else ""


def parse_resultados_esaj(html_pagina):
    """Retorna (registros, total) a partir do HTML de uma página de resultados eSAJ."""
    m_total = TOTAL_RE.search(html_pagina)
    total = int(re.sub(r'\D', '', m_total.group(1))) if m_total else 0
    registros = []
    for m in LINHA_RE.finditer(html_pagina):
        bloco = m.group(1)
        m_cd = CD_ACORDAO_RE.search(bloco)
        if not m_cd:
            continue
        cd_acordao = m_cd.group(1)
        m_foro = CD_FORO_RE.search(bloco)
        cd_foro = m_foro.group(1) if m_foro else "0"
        registros.append({
            "numero_processo": _extrair_numero(bloco),
            "relator": _campo(bloco, "Relator"),
            "orgao_julgador": _campo(bloco, "Órgão julgador"),
            "comarca": _campo(bloco, "Comarca"),
            "data_julgamento": _campo(bloco, "Data do julgamento"),
            "classe": _campo(bloco, "Classe"),
            "doc_id": f"{cd_acordao}:{cd_foro}",
            "tipo_doc": "acordao",
        })
    return registros, total


def tipo_do_documento_esaj(_linha):
    """Para eSAJ o tipo é sempre 'acordao'; mantido por simetria com TJSC."""
    return "acordao"


# ---------------------------------------------------------------------------
# AdaptadorEsaj
# ---------------------------------------------------------------------------

class AdaptadorEsaj(AdaptadorTribunal):
    """Adaptador concreto para portais eSAJ.

    Subclasses só precisam definir BASE_PADRAO (e opcionalmente SIGLA):

        class AdaptadorTjsp(AdaptadorEsaj):
            BASE_PADRAO = "https://esaj.tjsp.jus.br"
    """

    BASE_PADRAO: str = ""  # obrigatório em subclasses ou via __init__(base=...)
    SIGLA: str = "esaj"

    def __init__(
        self,
        base=None,
        user_agent=None,
        min_intervalo=2.0,
        max_tentativas=5,
        ps=50,
        max_paginas=100,
    ):
        self.base = (base or self.BASE_PADRAO).rstrip("/")
        if not self.base:
            raise ValueError("base URL obrigatória (defina BASE_PADRAO ou passe base=...)")
        self.ps = ps
        self.max_paginas = max_paginas
        self._user_agent = user_agent or USER_AGENT_PADRAO
        self.cliente = ClienteHttp(
            user_agent=self._user_agent,
            min_intervalo=min_intervalo,
            max_tentativas=max_tentativas,
        )
        self._conversation_id = ""

    # ------------------------------------------------------------------
    # Geração de URLs
    # ------------------------------------------------------------------

    def url_busca(self):
        return f"{self.base}/cjsg/resultadoCompleta.do"

    def url_consulta(self):
        return f"{self.base}/cjsg/consulta.do"

    def url_arquivo(self, cd_acordao, cd_foro):
        return f"{self.base}/cjsg/getArquivo.do?cdAcordao={cd_acordao}&cdForo={cd_foro}"

    # ------------------------------------------------------------------
    # AdaptadorTribunal
    # ------------------------------------------------------------------

    def verificar_robots(self) -> bool:
        partes = urlsplit(self.base)
        url_robots = f"{partes.scheme}://{partes.netloc}/robots.txt"
        try:
            resposta = self.cliente.get(url_robots, timeout=30)
        except requests.HTTPError as exc:
            codigo = exc.response.status_code if exc.response is not None else 0
            if 400 <= codigo < 500:
                logging.info("robots.txt indisponível (HTTP %s); sem restrições.", codigo)
                return True
            logging.warning("falha ao ler robots.txt (HTTP %s); prosseguindo.", codigo)
            return True
        except requests.RequestException as exc:
            logging.warning("falha ao ler robots.txt (%s); prosseguindo.", exc)
            return True
        analisador = urllib.robotparser.RobotFileParser()
        analisador.parse(resposta.text.splitlines())
        rotas = [self.url_busca(), self.url_consulta()]
        bloqueadas = [r for r in rotas if not analisador.can_fetch(self._user_agent, r)]
        if bloqueadas:
            logging.error("robots.txt desautoriza as rotas: %s", ", ".join(bloqueadas))
            return False
        logging.info("robots.txt lido: rotas necessárias permitidas.")
        return True

    def estabelecer_sessao(self) -> None:
        """GET na página de busca para extrair o conversationId da sessão."""
        resposta = self.cliente.get(self.url_consulta(), timeout=30)
        html = texto_resposta(resposta)
        m = CONV_ID_RE.search(html)
        self._conversation_id = m.group(1) if m else ""
        logging.debug("eSAJ conversationId=%r", self._conversation_id)

    def parsear_resultados(self, html: str) -> "tuple[list[dict], int]":
        return parse_resultados_esaj(html)

    def buscar(self, relator, params, *, ps=None):
        """GET paginado em resultadoCompleta.do com parâmetros eSAJ."""
        ps = ps if ps is not None else self.ps
        pagina = 1
        while pagina <= self.max_paginas:
            query = {
                "dados.buscaInteiroTeor": "",
                "dados.pesquisarPor": "ementa",
                "dados.nomeRelator": relator,
                "dados.dtJulgamentoInicio": "",
                "dados.dtJulgamentoFim": "",
                "dados.cdCamaraIsolada": "",
                "paginaConsulta": str(pagina),
                "registrosPorPagina": str(ps),
                "conversationId": self._conversation_id,
            }
            query.update(params)
            resposta = self.cliente.get(self.url_busca(), params=query, timeout=60)
            html_pagina = texto_resposta(resposta)
            registros, total = parse_resultados_esaj(html_pagina)
            yield registros, total, pagina, html_pagina
            if not registros:
                break
            if total and pagina * ps >= total:
                break
            if not total and len(registros) < ps:
                break
            pagina += 1

    def baixar_inteiro_teor(self, doc_id, tipo, base_destino) -> InteiroTeor:
        """Baixa PDF via getArquivo.do; fallback para HTML se não vier PDF."""
        partes = doc_id.split(":", 1)
        cd_acordao = partes[0]
        cd_foro = partes[1] if len(partes) > 1 else "0"
        url = self.url_arquivo(cd_acordao, cd_foro)
        resposta = self.cliente.get(url, stream=True, timeout=120)
        formato, primeiro, iterador = classificar_resposta(resposta)
        if formato:
            caminho = base_destino + "." + formato
            salvar_binario(caminho, primeiro, iterador)
            resposta.close()
            return InteiroTeor(caminho=caminho, formato=formato, url=url)
        # Se não veio binário direto, consome a resposta como HTML
        corpo = primeiro + b"".join(iterador)
        codificacao = resposta.encoding or "utf-8"
        resposta.close()
        caminho = base_destino + ".html"
        salvar_texto(caminho, corpo.decode(codificacao, errors="replace"))
        return InteiroTeor(caminho=caminho, formato="html", url=url)
