#!/usr/bin/env python3
"""Testes offline de leis/planalto.py contra servidor planalto.gov.br falso.

Roda com: python test_leis.py -v

O stub simula o contrato real do portal:
  - robots.txt permissivo ou proibitivo
  - GET /ccivil_03/...lei.htm → HTML com artigos numerados no formato planalto
  - Encoding UTF-8 com charset declarado no Content-Type
"""

import importlib
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------------------
# HTML de exemplo fiel ao formato planalto.gov.br
# ---------------------------------------------------------------------------

def _lei_html(titulo, numero, artigos_dict):
    """Gera HTML no formato planalto com os artigos fornecidos."""
    artigos_html = ""
    for num, texto in artigos_dict.items():
        artigos_html += f"<p><strong>Art. {num}º</strong> {texto}</p>\n"
    return (
        "<!DOCTYPE html><html><head>"
        f"<title>Lei {numero}</title>"
        "</head><body>"
        '<div id="conteudo">'
        f"<p><strong>{titulo}</strong></p>"
        + artigos_html
        + "</div></body></html>"
    )


CPC_ARTIGOS = {
    "1": "As normas processuais civis previstas nesta Lei serão aplicadas expressamente.",
    "2": "O processo começa por iniciativa da parte e se desenvolve por impulso oficial.",
    "300": "A tutela de urgência será concedida quando houver elementos que evidenciem "
           "a probabilidade do direito e o perigo de dano ou o risco ao resultado útil "
           "do processo.",
    "301": "A tutela de evidência será concedida, independentemente da demonstração de "
           "perigo de dano ou de risco ao resultado útil do processo.",
}

CC_ARTIGOS = {
    "1": "Toda pessoa é capaz de direitos e deveres na ordem civil.",
    "1566": "São deveres de ambos os cônjuges.",
    "1694": "Podem os parentes, os cônjuges ou companheiros pedir uns aos outros os "
             "alimentos de que necessitem para viver de modo compatível com a sua "
             "condição social.",
    "1695": "São devidos os alimentos quando quem os pretende não tem bens suficientes.",
}

LEI_ALIM_ARTIGOS = {
    "1": "Os alimentos de que trata esta lei são os previstos no art. 1.694 e seguintes.",
    "2": "O credor, pessoalmente, ou por intermédio de advogado, dirigir-se-á ao juiz.",
    "4": "Dispensar-se-á a audiência de instrução e julgamento se os elementos constantes.",
}

LEI_HTML: dict[str, str] = {
    "CPC": _lei_html("CÓDIGO DE PROCESSO CIVIL", "13.105/2015", CPC_ARTIGOS),
    "CC": _lei_html("CÓDIGO CIVIL", "10.406/2002", CC_ARTIGOS),
    "Lei_Alimentos": _lei_html("LEI DE ALIMENTOS", "5.478/1968", LEI_ALIM_ARTIGOS),
}

# ---------------------------------------------------------------------------
# Servidor falso
# ---------------------------------------------------------------------------

CAMINHOS = {
    "/ccivil_03/_ato2015-2018/2015/lei/l13105.htm": "CPC",
    "/ccivil_03/leis/2002/l10406compilado.htm": "CC",
    "/ccivil_03/leis/l5478.htm": "Lei_Alimentos",
}


class PortalPlanalto(BaseHTTPRequestHandler):
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
        if rota.path == "/robots.txt":
            corpo = ("User-agent: *\nDisallow: /\n" if self.server.bloqueia
                     else "User-agent: *\nDisallow:\n")
            self._ok(corpo, "text/plain")
        elif rota.path in CAMINHOS:
            nome = CAMINHOS[rota.path]
            self._ok(LEI_HTML[nome])
        else:
            self.send_error(404)


class ServidorPlanalto(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.bloqueia = False


# ---------------------------------------------------------------------------
# Testes: funções puras de leis/planalto.py
# ---------------------------------------------------------------------------

class TesteFuncoesPuras(unittest.TestCase):
    """Testa _html_para_texto e extrair_artigos sem rede."""

    def setUp(self):
        from leis import planalto as m
        self.m = m

    def test_html_para_texto_extrai_conteudo(self):
        html = LEI_HTML["CPC"]
        texto = self.m._html_para_texto(html)
        self.assertIn("Art.", texto)
        self.assertIn("tutela de urgência", texto.lower())
        # Scripts e estilos devem ter sido removidos
        self.assertNotIn("<script", texto)
        self.assertNotIn("<style", texto)

    def test_html_para_texto_ignora_tags(self):
        html = "<html><body><div id='conteudo'><p>Art. 1º Texto.</p></div></body></html>"
        texto = self.m._html_para_texto(html)
        self.assertNotIn("<p>", texto)
        self.assertIn("Art. 1", texto)

    def test_extrair_artigos_numera_corretamente(self):
        texto = (
            "Art. 1º Texto do primeiro artigo.\n"
            "Parágrafo único. Texto do parágrafo.\n"
            "Art. 2º Texto do segundo artigo.\n"
            "Art. 300º Texto do trezentos.\n"
        )
        artigos = self.m.extrair_artigos(texto)
        self.assertIn("1", artigos)
        self.assertIn("2", artigos)
        self.assertIn("300", artigos)
        self.assertEqual(len(artigos), 3)

    def test_extrair_artigos_inclui_paragrafos(self):
        texto = (
            "Art. 1º Caput.\n"
            "§ 1º Parágrafo primeiro.\n"
            "§ 2º Parágrafo segundo.\n"
            "Art. 2º Segundo artigo.\n"
        )
        artigos = self.m.extrair_artigos(texto)
        self.assertIn("§ 1º", artigos["1"])
        self.assertIn("§ 2º", artigos["1"])
        self.assertNotIn("§", artigos["2"])

    def test_extrair_artigos_cpc_html(self):
        texto = self.m._html_para_texto(LEI_HTML["CPC"])
        artigos = self.m.extrair_artigos(texto)
        self.assertIn("1", artigos)
        self.assertIn("300", artigos)
        self.assertIn("tutela de urgência", artigos["300"].lower())

    def test_extrair_artigos_vazio(self):
        self.assertEqual(self.m.extrair_artigos("Sem artigos aqui."), {})

    def test_extrair_artigos_sufixo_letra(self):
        # Artigos intercalados como "Art. 1-A" (CC) — sufixo letra maiúscula
        texto = "Art. 1-A Texto do artigo intercalado.\nArt. 2º Seguinte.\n"
        artigos = self.m.extrair_artigos(texto)
        self.assertIn("1-A", artigos)
        self.assertIn("2", artigos)


# ---------------------------------------------------------------------------
# Testes: I/O do repositório
# ---------------------------------------------------------------------------

class TesteRepositorio(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="leis_repo_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        from leis import planalto as m
        self.m = m

    def _dados(self, nome="CPC", num_art=3):
        return {
            "lei": nome,
            "nome": "Código de Processo Civil",
            "numero": "13.105",
            "ano": 2015,
            "url": "http://fake/",
            "hash": "abc123",
            "atualizado_em": "2026-01-01",
            "vigente": True,
            "artigos": {str(i): f"Texto art {i}" for i in range(1, num_art + 1)},
        }

    def test_salvar_e_carregar_roundtrip(self):
        dados = self._dados()
        self.m.salvar_lei(dados, self.tmp)
        carregado = self.m.carregar_lei("CPC", self.tmp)
        self.assertIsNotNone(carregado)
        self.assertEqual(carregado["hash"], "abc123")
        self.assertEqual(carregado["artigos"]["1"], "Texto art 1")

    def test_carregar_inexistente_retorna_none(self):
        self.assertIsNone(self.m.carregar_lei("CPC", self.tmp))

    def test_salvar_retorna_true_em_primeira_vez(self):
        dados = self._dados()
        self.assertTrue(self.m.salvar_lei(dados, self.tmp))

    def test_salvar_retorna_false_sem_mudanca(self):
        dados = self._dados()
        self.m.salvar_lei(dados, self.tmp)
        self.assertFalse(self.m.salvar_lei(dados, self.tmp))

    def test_salvar_retorna_true_com_hash_diferente(self):
        dados = self._dados()
        self.m.salvar_lei(dados, self.tmp)
        dados2 = self._dados()
        dados2["hash"] = "xyz999"
        self.assertTrue(self.m.salvar_lei(dados2, self.tmp))

    def test_historico_jsonl_criado(self):
        self.m.salvar_lei(self._dados(), self.tmp)
        hist = os.path.join(self.tmp, "CPC_historico.jsonl")
        self.assertTrue(os.path.exists(hist))
        with open(hist, encoding="utf-8") as f:
            linha = json.loads(f.readline())
        self.assertEqual(linha["hash"], "abc123")

    def test_historico_acumula_versoes(self):
        d1 = self._dados()
        d1["hash"] = "v1"
        d2 = self._dados()
        d2["hash"] = "v2"
        self.m.salvar_lei(d1, self.tmp)
        self.m.salvar_lei(d2, self.tmp)
        hist = os.path.join(self.tmp, "CPC_historico.jsonl")
        with open(hist, encoding="utf-8") as f:
            linhas = f.readlines()
        self.assertEqual(len(linhas), 2)

    def test_artigo_texto_retorna_texto(self):
        self.m.salvar_lei(self._dados(), self.tmp)
        texto = self.m.artigo_texto("CPC", "1", self.tmp)
        self.assertEqual(texto, "Texto art 1")

    def test_artigo_texto_ausente_retorna_vazio(self):
        self.m.salvar_lei(self._dados(), self.tmp)
        self.assertEqual(self.m.artigo_texto("CPC", "999", self.tmp), "")

    def test_artigo_texto_lei_nao_cacheada_retorna_vazio(self):
        self.assertEqual(self.m.artigo_texto("CC", "1", self.tmp), "")

    def test_artigo_texto_aceita_inteiro(self):
        self.m.salvar_lei(self._dados(), self.tmp)
        self.assertEqual(self.m.artigo_texto("CPC", 1, self.tmp), "Texto art 1")


# ---------------------------------------------------------------------------
# Testes: scraping contra portal falso
# ---------------------------------------------------------------------------

class TesteBuscarLei(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.servidor = ServidorPlanalto(("127.0.0.1", 0), PortalPlanalto)
        porta = cls.servidor.server_address[1]
        threading.Thread(target=cls.servidor.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{porta}"
        os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
        os.environ["PLANALTO_BASE"] = cls.base
        os.environ["PLANALTO_MIN_INTERVALO"] = "0"
        # Força reimport para capturar PLANALTO_BASE atualizado
        import importlib
        import leis.planalto
        importlib.reload(leis.planalto)
        import leis
        importlib.reload(leis)
        cls.m = leis.planalto

    @classmethod
    def tearDownClass(cls):
        cls.servidor.shutdown()
        cls.servidor.server_close()

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="leis_busca_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.servidor.bloqueia = False

    def _cliente(self):
        from nucleo.http import ClienteHttp
        return ClienteHttp(user_agent="Test/1.0", min_intervalo=0.0, max_tentativas=3)

    def test_buscar_cpc(self):
        dados = self.m.buscar_lei("CPC", self._cliente(), base=self.base)
        self.assertEqual(dados["lei"], "CPC")
        self.assertEqual(dados["numero"], "13.105")
        self.assertEqual(dados["ano"], 2015)
        self.assertTrue(dados["vigente"])
        self.assertIn("1", dados["artigos"])
        self.assertIn("300", dados["artigos"])
        self.assertIn("tutela de urgência", dados["artigos"]["300"].lower())

    def test_buscar_cc(self):
        dados = self.m.buscar_lei("CC", self._cliente(), base=self.base)
        self.assertIn("1694", dados["artigos"])
        self.assertIn("alimentos", dados["artigos"]["1694"].lower())

    def test_buscar_lei_alimentos(self):
        dados = self.m.buscar_lei("Lei_Alimentos", self._cliente(), base=self.base)
        self.assertIn("1", dados["artigos"])
        self.assertGreaterEqual(len(dados["artigos"]), 1)

    def test_hash_deterministico(self):
        c = self._cliente()
        d1 = self.m.buscar_lei("CPC", c, base=self.base)
        d2 = self.m.buscar_lei("CPC", c, base=self.base)
        self.assertEqual(d1["hash"], d2["hash"])

    def test_url_lei_usa_base_correta(self):
        url = self.m.url_lei("CPC", self.base)
        self.assertTrue(url.startswith(self.base))
        self.assertIn("l13105", url)

    def test_verificar_robots_permitido(self):
        self.servidor.bloqueia = False
        self.assertTrue(self.m._verificar_robots(self._cliente(), self.base))

    def test_verificar_robots_bloqueado(self):
        self.servidor.bloqueia = True
        self.assertFalse(self.m._verificar_robots(self._cliente(), self.base))

    def test_salvar_e_recuperar_via_artigo_texto(self):
        dados = self.m.buscar_lei("CPC", self._cliente(), base=self.base)
        self.m.salvar_lei(dados, self.tmp)
        texto = self.m.artigo_texto("CPC", "300", self.tmp)
        self.assertIn("tutela de urgência", texto.lower())


# ---------------------------------------------------------------------------
# Testes: CLI (main)
# ---------------------------------------------------------------------------

class TesteCLI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Reutiliza o mesmo servidor da suite anterior, se disponível,
        # ou cria um novo.
        cls.servidor = ServidorPlanalto(("127.0.0.1", 0), PortalPlanalto)
        porta = cls.servidor.server_address[1]
        threading.Thread(target=cls.servidor.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{porta}"
        os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
        os.environ["PLANALTO_BASE"] = cls.base
        os.environ["PLANALTO_MIN_INTERVALO"] = "0"
        import importlib
        import leis.planalto
        importlib.reload(leis.planalto)
        cls.m = leis.planalto

    @classmethod
    def tearDownClass(cls):
        cls.servidor.shutdown()
        cls.servidor.server_close()

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="leis_cli_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.servidor.bloqueia = False

    def test_listar(self, capsys=None):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.m.main(["--listar", "--out", self.tmp])
        saida = buf.getvalue()
        self.assertIn("CPC", saida)
        self.assertIn("CC", saida)
        self.assertIn("Lei_Alimentos", saida)

    def test_baixar_uma_lei(self):
        self.m.main(["CPC", "--out", self.tmp])
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "CPC.json")))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "CC.json")))

    def test_baixar_todas(self):
        self.m.main(["--out", self.tmp])
        for nome in self.m.LEIS:
            self.assertTrue(os.path.exists(os.path.join(self.tmp, f"{nome}.json")),
                            f"{nome}.json deveria existir após baixar todas")

    def test_idempotente_segundo_download_nao_muda_historico(self):
        self.m.main(["CPC", "--out", self.tmp])
        self.m.main(["CPC", "--out", self.tmp])
        hist = os.path.join(self.tmp, "CPC_historico.jsonl")
        with open(hist, encoding="utf-8") as f:
            linhas = f.readlines()
        self.assertEqual(len(linhas), 1, "conteúdo idêntico não deve gerar nova entrada no histórico")

    def test_mostrar_artigo(self):
        self.m.main(["CPC", "--out", self.tmp])
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.m.main(["--mostrar", "CPC", "300", "--out", self.tmp])
        saida = buf.getvalue()
        self.assertIn("tutela de urgência", saida.lower())

    def test_mostrar_artigo_inexistente(self):
        self.m.main(["CPC", "--out", self.tmp])
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.m.main(["--mostrar", "CPC", "9999", "--out", self.tmp])
        self.assertIn("não encontrado", buf.getvalue())

    def test_robots_bloqueado_aborta(self):
        self.servidor.bloqueia = True
        with self.assertRaises(SystemExit) as ctx:
            self.m.main(["CPC", "--out", self.tmp])
        self.assertEqual(ctx.exception.code, 2)

    def test_lei_desconhecida_aborta(self):
        with self.assertRaises(SystemExit):
            self.m.main(["LEI_INEXISTENTE", "--out", self.tmp])


if __name__ == "__main__":
    unittest.main(verbosity=2)
