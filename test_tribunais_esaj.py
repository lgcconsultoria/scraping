#!/usr/bin/env python3
"""Testes offline do AdaptadorEsaj / AdaptadorTjsp / AdaptadorTjms contra portal falso.

Roda com: python test_tribunais_esaj.py -v

O stub simula o contrato REAL do portal eSAJ/CJSG (validado em snapshots de
produção):
  - GET /cjsg/consulta.do → HTML com conversationId oculto
  - GET /robots.txt → permissivo ou proibitivo
  - GET /cjsg/resultadoCompleta.do → resultados paginados no formato eSAJ
    (total em "Acórdãos (N)", linhas em <tr id="id-doc-N-acordao">)
  - GET /cjsg/getArquivo.do?cdAcordao=NNN&cdForo=NNN → PDF ou HTML fallback
"""

import os
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tribunais.base import AdaptadorTribunal, InteiroTeor
from tribunais.esaj import (AdaptadorEsaj, parse_resultados_esaj,
                              tipo_do_documento_esaj)
from tribunais.tjsp import AdaptadorTjsp
from tribunais.tjms import AdaptadorTjms

PDF_BYTES = b"%PDF-1.4 fake-esaj"
RTF_BYTES = b"{\\rtf1 fake-esaj}"

CONV_ID = "CONV_TESTE_123"
RELATOR_OK = "Desembargador Teste"

NUM_A = "5000001-11.2024.8.26.0000"
NUM_B = "5000002-22.2024.8.26.0000"
NUM_C = "5000003-33.2024.8.26.0000"

CD_PDF = "100001"
CD_HTML = "100002"     # getArquivo devolve HTML (sem PDF)
CD_FORO = "990"


def linha_resultado(numero, cd_acordao, cd_foro=CD_FORO, n=1,
                    relator=RELATOR_OK, orgao="1ª Câmara de Direito Privado",
                    comarca="São Paulo", data="15/01/2024",
                    classe="Agravo de Instrumento"):
    return (
        f'<tr class="fundocinza" id="id-doc-{n}-acordao">'
        f'<td class="numProcesso"><a href="#">{numero}</a></td>'
        f'<td>'
        f'<strong>Relator(a):</strong> {relator}<br>'
        f'<strong>Comarca:</strong> {comarca}<br>'
        f'<strong>Órgão julgador:</strong> {orgao}<br>'
        f'<strong>Data do julgamento:</strong> {data}<br>'
        f'<strong>Classe:</strong> {classe}<br>'
        f'</td>'
        f'<td><a href="getArquivo.do?cdAcordao={cd_acordao}&cdForo={cd_foro}">'
        f'Inteiro Teor</a></td>'
        f'</tr>'
    )


def pagina_resultados_esaj(total, linhas_html):
    return (
        f'<html><body>'
        f'<span id="nomeAba-A">Acórdãos ({total})</span>'
        f'<table>{linhas_html}</table>'
        f'</body></html>'
    )


PAGINA_VAZIA = '<html><body><span id="nomeAba-A">Acórdãos (0)</span></body></html>'

PAGINA_CONSULTA = (
    '<html><body>'
    f'<form action="resultadoCompleta.do">'
    f'<input type="hidden" name="conversationId" value="{CONV_ID}">'
    f'</form>'
    f'</body></html>'
)


class PortalEsaj(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _ok(self, dados, ct="text/html; charset=utf-8"):
        if isinstance(dados, str):
            dados = dados.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(dados)))
        self.end_headers()
        self.wfile.write(dados)

    def do_GET(self):
        rota = urlsplit(self.path)
        params = parse_qs(rota.query)

        if rota.path == "/robots.txt":
            corpo = ("User-agent: *\nDisallow: /\n" if self.server.bloqueia
                     else "User-agent: *\nDisallow:\n")
            self._ok(corpo, "text/plain")

        elif rota.path == "/cjsg/consulta.do":
            self._ok(PAGINA_CONSULTA)

        elif rota.path == "/cjsg/resultadoCompleta.do":
            relator = params.get("dados.nomeRelator", [""])[0]
            conv = params.get("conversationId", [""])[0]
            pagina = int(params.get("paginaConsulta", ["1"])[0])
            ps = int(params.get("registrosPorPagina", ["50"])[0])

            if relator != RELATOR_OK or conv != CONV_ID:
                self._ok(PAGINA_VAZIA)
                return

            # total=3 com ps=2 → 2 páginas
            if ps >= 50:
                # ps grande: devolve tudo de uma vez
                html = pagina_resultados_esaj(
                    3,
                    linha_resultado(NUM_A, CD_PDF, n=1)
                    + linha_resultado(NUM_B, CD_HTML, n=2)
                    + linha_resultado(NUM_C, CD_PDF, n=3))
            elif pagina == 1:
                html = pagina_resultados_esaj(
                    3,
                    linha_resultado(NUM_A, CD_PDF, n=1)
                    + linha_resultado(NUM_B, CD_HTML, n=2))
            else:
                html = pagina_resultados_esaj(
                    3,
                    linha_resultado(NUM_C, CD_PDF, n=1))
            self._ok(html)

        elif rota.path == "/cjsg/getArquivo.do":
            cd = params.get("cdAcordao", [""])[0]
            if cd == CD_PDF:
                self._ok(PDF_BYTES, "application/pdf")
            elif cd == CD_HTML:
                self._ok("<html><body>Acórdão em HTML</body></html>")
            else:
                self.send_error(404)

        else:
            self.send_error(404)


class ServidorEsaj(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.bloqueia = False


# ---------------------------------------------------------------------------
# Testes: nucleo.cache.salvar_binario e nucleo.http.classificar_resposta
# ---------------------------------------------------------------------------

class TesteNucleoNovos(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nucleo_bin_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_salvar_binario_atomico(self):
        from nucleo.cache import salvar_binario
        caminho = os.path.join(self.tmp, "arq.pdf")
        salvar_binario(caminho, b"%PDF", iter([b"-1.4", b" fake"]))
        self.assertFalse(os.path.exists(caminho + ".part"))
        with open(caminho, "rb") as f:
            self.assertEqual(f.read(), b"%PDF-1.4 fake")

    def test_classificar_resposta_pdf(self):
        from nucleo.http import classificar_resposta
        import unittest.mock as mock
        dados = b"%PDF-1.4 content"
        resposta = mock.MagicMock()
        resposta.headers = {"Content-Type": "application/pdf"}
        resposta.iter_content.return_value = iter([dados])
        formato, primeiro, _ = classificar_resposta(resposta)
        self.assertEqual(formato, "pdf")
        self.assertEqual(primeiro, dados)

    def test_classificar_resposta_rtf_por_magic_bytes(self):
        from nucleo.http import classificar_resposta
        import unittest.mock as mock
        dados = b"{\\rtf1 content}"
        resposta = mock.MagicMock()
        resposta.headers = {"Content-Type": "text/html"}  # CT errado
        resposta.iter_content.return_value = iter([dados])
        formato, _, _ = classificar_resposta(resposta)
        self.assertEqual(formato, "rtf")

    def test_classificar_resposta_html(self):
        from nucleo.http import classificar_resposta
        import unittest.mock as mock
        resposta = mock.MagicMock()
        resposta.headers = {"Content-Type": "text/html"}
        resposta.iter_content.return_value = iter([b"<html>"])
        formato, _, _ = classificar_resposta(resposta)
        self.assertEqual(formato, "")


# ---------------------------------------------------------------------------
# Testes: funções puras de tribunais/esaj.py
# ---------------------------------------------------------------------------

class TesteFuncoesEsaj(unittest.TestCase):
    def _bloco(self, numero=NUM_A, cd_acordao=CD_PDF, n=1):
        return linha_resultado(numero, cd_acordao, n=n)

    def test_parse_total_com_ponto(self):
        html = f'<span id="nomeAba-A">Acórdãos (1.234)</span>'
        _, total = parse_resultados_esaj(html)
        self.assertEqual(total, 1234)

    def test_parse_zero_resultados(self):
        registros, total = parse_resultados_esaj(PAGINA_VAZIA)
        self.assertEqual(registros, [])
        self.assertEqual(total, 0)

    def test_parse_extrai_campos(self):
        html = pagina_resultados_esaj(1, self._bloco())
        registros, total = parse_resultados_esaj(html)
        self.assertEqual(total, 1)
        self.assertEqual(len(registros), 1)
        r = registros[0]
        self.assertEqual(r["numero_processo"], NUM_A)
        self.assertEqual(r["relator"], RELATOR_OK)
        self.assertEqual(r["orgao_julgador"], "1ª Câmara de Direito Privado")
        self.assertEqual(r["comarca"], "São Paulo")
        self.assertEqual(r["data_julgamento"], "15/01/2024")
        self.assertEqual(r["classe"], "Agravo de Instrumento")
        self.assertEqual(r["doc_id"], f"{CD_PDF}:{CD_FORO}")
        self.assertEqual(r["tipo_doc"], "acordao")

    def test_parse_multiplos_resultados(self):
        html = pagina_resultados_esaj(
            2,
            self._bloco(NUM_A, CD_PDF, n=1) + self._bloco(NUM_B, CD_HTML, n=2))
        registros, total = parse_resultados_esaj(html)
        self.assertEqual(total, 2)
        self.assertEqual(len(registros), 2)
        self.assertEqual(registros[1]["doc_id"], f"{CD_HTML}:{CD_FORO}")

    def test_tipo_do_documento_sempre_acordao(self):
        self.assertEqual(tipo_do_documento_esaj({}), "acordao")
        self.assertEqual(tipo_do_documento_esaj({"url_integra": "http://x/y"}), "acordao")

    def test_doc_id_encoda_cd_acordao_e_foro(self):
        html = pagina_resultados_esaj(1, self._bloco(NUM_A, "999888"))
        registros, _ = parse_resultados_esaj(html)
        self.assertEqual(registros[0]["doc_id"], f"999888:{CD_FORO}")


# ---------------------------------------------------------------------------
# Testes: AdaptadorEsaj contra portal falso
# ---------------------------------------------------------------------------

class TesteAdaptadorEsaj(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.servidor = ServidorEsaj(("127.0.0.1", 0), PortalEsaj)
        porta = cls.servidor.server_address[1]
        threading.Thread(target=cls.servidor.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{porta}"
        os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"

    @classmethod
    def tearDownClass(cls):
        cls.servidor.shutdown()
        cls.servidor.server_close()

    def _ad(self, **kw):
        return AdaptadorEsaj(base=self.base, user_agent="TestAgent/1.0",
                              min_intervalo=0.0, max_tentativas=3, **kw)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="esaj_adapt_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.servidor.bloqueia = False

    # ABC -------------------------------------------------------------------

    def test_esaj_implementa_abc(self):
        ad = self._ad()
        self.assertIsInstance(ad, AdaptadorTribunal)

    def test_base_obrigatorio_sem_base_padrao(self):
        with self.assertRaises(ValueError):
            AdaptadorEsaj()  # sem base e sem BASE_PADRAO

    # robots.txt ------------------------------------------------------------

    def test_verificar_robots_permitido(self):
        self.servidor.bloqueia = False
        self.assertTrue(self._ad().verificar_robots())

    def test_verificar_robots_bloqueado(self):
        self.servidor.bloqueia = True
        self.assertFalse(self._ad().verificar_robots())

    # sessão ----------------------------------------------------------------

    def test_estabelecer_sessao_extrai_conversation_id(self):
        ad = self._ad()
        ad.estabelecer_sessao()
        self.assertEqual(ad._conversation_id, CONV_ID)

    def test_sessao_ausente_usa_string_vazia(self):
        ad = self._ad()
        # Sem chamar estabelecer_sessao, _conversation_id deve ser ""
        self.assertEqual(ad._conversation_id, "")

    # buscar ----------------------------------------------------------------

    def test_buscar_requer_sessao(self):
        ad = self._ad()
        # Sem sessão, conversationId="" → portal devolve vazio
        resultados = list(ad.buscar(RELATOR_OK, {}))
        registros, total, _, _ = resultados[0]
        self.assertEqual(registros, [])

    def test_buscar_com_sessao_retorna_registros(self):
        ad = self._ad()
        ad.estabelecer_sessao()
        resultados = list(ad.buscar(RELATOR_OK, {}))
        self.assertEqual(len(resultados), 1)
        registros, total, pagina, _ = resultados[0]
        self.assertEqual(total, 3)
        self.assertEqual(len(registros), 3)
        self.assertEqual(pagina, 1)

    def test_buscar_paginacao(self):
        ad = self._ad(ps=2)
        ad.estabelecer_sessao()
        resultados = list(ad.buscar(RELATOR_OK, {}))
        self.assertEqual(len(resultados), 2, "total=3, ps=2 → deve pedir 2 páginas")
        todos = [r for (regs, _, _, _) in resultados for r in regs]
        self.assertEqual(len(todos), 3)

    def test_buscar_relator_errado_retorna_vazio(self):
        ad = self._ad()
        ad.estabelecer_sessao()
        resultados = list(ad.buscar("Nome Inexistente", {}))
        registros, total, _, _ = resultados[0]
        self.assertEqual(registros, [])

    def test_buscar_params_extras_passados(self):
        ad = self._ad()
        ad.estabelecer_sessao()
        # Passa parâmetro adicional; portal ignora mas não deve quebrar
        resultados = list(ad.buscar(RELATOR_OK, {"dados.buscaInteiroTeor": "alimentos"}))
        self.assertEqual(len(resultados), 1)

    # baixar_inteiro_teor ---------------------------------------------------

    def test_baixar_pdf(self):
        ad = self._ad()
        base = os.path.join(self.tmp, "doc")
        teor = ad.baixar_inteiro_teor(f"{CD_PDF}:{CD_FORO}", "acordao", base)
        self.assertIsInstance(teor, InteiroTeor)
        self.assertEqual(teor.formato, "pdf")
        self.assertTrue(teor.caminho.endswith(".pdf"))
        with open(teor.caminho, "rb") as f:
            self.assertTrue(f.read().startswith(b"%PDF"))

    def test_baixar_html_fallback(self):
        ad = self._ad()
        base = os.path.join(self.tmp, "doc_html")
        teor = ad.baixar_inteiro_teor(f"{CD_HTML}:{CD_FORO}", "acordao", base)
        self.assertEqual(teor.formato, "html")
        with open(teor.caminho, encoding="utf-8") as f:
            self.assertIn("Acórdão", f.read())

    def test_url_arquivo_correto(self):
        ad = self._ad()
        url = ad.url_arquivo("999888", "990")
        self.assertIn("cdAcordao=999888", url)
        self.assertIn("cdForo=990", url)

    # ps e max_paginas ------------------------------------------------------

    def test_ps_customizado(self):
        ad = AdaptadorEsaj(base=self.base, user_agent="U", min_intervalo=0.0, ps=20)
        self.assertEqual(ad.ps, 20)

    def test_max_paginas_padrao(self):
        self.assertEqual(self._ad().max_paginas, 100)


# ---------------------------------------------------------------------------
# Testes: subclasses TJSP e TJMS
# ---------------------------------------------------------------------------

class TesteSubclasses(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.servidor = ServidorEsaj(("127.0.0.1", 0), PortalEsaj)
        porta = cls.servidor.server_address[1]
        threading.Thread(target=cls.servidor.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{porta}"
        os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"

    @classmethod
    def tearDownClass(cls):
        cls.servidor.shutdown()
        cls.servidor.server_close()

    def setUp(self):
        self.servidor.bloqueia = False

    def test_tjsp_sigla_e_base_padrao(self):
        self.assertEqual(AdaptadorTjsp.SIGLA, "tjsp")
        self.assertIn("tjsp.jus.br", AdaptadorTjsp.BASE_PADRAO)

    def test_tjms_sigla_e_base_padrao(self):
        self.assertEqual(AdaptadorTjms.SIGLA, "tjms")
        self.assertIn("tjms.jus.br", AdaptadorTjms.BASE_PADRAO)

    def test_tjsp_herda_adaptador_esaj(self):
        ad = AdaptadorTjsp(base=self.base, user_agent="U", min_intervalo=0.0)
        self.assertIsInstance(ad, AdaptadorEsaj)
        self.assertIsInstance(ad, AdaptadorTribunal)

    def test_tjms_herda_adaptador_esaj(self):
        ad = AdaptadorTjms(base=self.base, user_agent="U", min_intervalo=0.0)
        self.assertIsInstance(ad, AdaptadorEsaj)

    def test_tjsp_usa_base_padrao_quando_nao_passada(self):
        ad = AdaptadorTjsp(user_agent="U", min_intervalo=0.0)
        self.assertEqual(ad.base, "https://esaj.tjsp.jus.br")

    def test_tjms_usa_base_padrao_quando_nao_passada(self):
        ad = AdaptadorTjms(user_agent="U", min_intervalo=0.0)
        self.assertEqual(ad.base, "https://esaj.tjms.jus.br")

    def test_tjsp_busca_com_sessao(self):
        ad = AdaptadorTjsp(base=self.base, user_agent="U", min_intervalo=0.0)
        ad.estabelecer_sessao()
        resultados = list(ad.buscar(RELATOR_OK, {}))
        registros, total, _, _ = resultados[0]
        self.assertEqual(total, 3)

    def test_tjms_busca_com_sessao(self):
        ad = AdaptadorTjms(base=self.base, user_agent="U", min_intervalo=0.0)
        ad.estabelecer_sessao()
        resultados = list(ad.buscar(RELATOR_OK, {}))
        registros, total, _, _ = resultados[0]
        self.assertEqual(total, 3)

    def test_tjsp_robots_bloqueado(self):
        self.servidor.bloqueia = True
        ad = AdaptadorTjsp(base=self.base, user_agent="U", min_intervalo=0.0)
        self.assertFalse(ad.verificar_robots())

    def test_tjsp_download_pdf(self):
        tmp = tempfile.mkdtemp()
        try:
            ad = AdaptadorTjsp(base=self.base, user_agent="U", min_intervalo=0.0)
            teor = ad.baixar_inteiro_teor(f"{CD_PDF}:{CD_FORO}", "acordao",
                                           os.path.join(tmp, "doc"))
            self.assertEqual(teor.formato, "pdf")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
