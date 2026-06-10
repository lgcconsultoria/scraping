#!/usr/bin/env python3
"""Testes offline do scraper_tjsc contra um portal falso local (sem rede externa).

Roda com: python test_scraper_tjsc.py -v

O stub reproduz o contrato do portal real: GET / para sessão, robots.txt,
POST buscaajax.do paginado com rótulos textuais ("Processo:", "Relatora:",
"Orgão Julgador:"...), e integra.do em três variantes: PDF direto, visualizador
HTML com link para o PDF real, e visualizador sem PDF (fallback html.do).
"""

import csv
import importlib
import os
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

ID_A = "1" * 30   # integra.do devolve PDF direto (Content-Type correto)
ID_B = "2" * 30   # integra.do devolve visualizador HTML com iframe -> PDF real
ID_C = "3" * 30   # integra.do sem PDF localizável -> fallback html.do
ID_D = "4" * 30   # PDF com Content-Type errado (text/html) -> magic bytes %PDF

NUM_A = "5000001-11.2024.8.24.0000"
NUM_B = "5000002-22.2024.8.24.0000"
NUM_C = "5000003-33.2024.8.24.0000"
NUM_D = NUM_A  # mesmo número de processo que A: testa colisão de nome de arquivo

PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"

RELATORA = "Cláudia Lambert de Faria"


def registro_html(numero, doc_id):
    """Um resultado de busca como o portal devolve (entidades HTML incluídas)."""
    return f"""
    <div class="resultado">
      <p><strong>Processo:</strong>
         <a href="/jurisprudencia/html.do?id={doc_id}&amp;categoria=acordao_eproc">{numero}</a>
         (Ac&oacute;rd&atilde;o do Tribunal de Justi&ccedil;a)</p>
      <p><strong>Relatora:</strong> Cl&aacute;udia Lambert de Faria</p>
      <p><strong>Origem:</strong> Florian&oacute;polis</p>
      <p><strong>Org&atilde;o Julgador:</strong> Nona C&acirc;mara de Direito Civil</p>
      <p><strong>Julgado em:</strong> 12/03/2025</p>
      <p><strong>Classe:</strong> Agravo de Instrumento</p>
      <p>Ementa: ALIMENTOS. TUTELA DE URG&Ecirc;NCIA. COGNI&Ccedil;&Atilde;O SUM&Aacute;RIA.</p>
      <a href="integra.do?rowid={doc_id}&amp;tipo=acordao_eproc">Inteiro teor</a>
    </div>"""


class Portal(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _responder(self, dados, content_type, extras=()):
        if isinstance(dados, str):
            dados = dados.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(dados)))
        for chave, valor in extras:
            self.send_header(chave, valor)
        self.end_headers()
        self.wfile.write(dados)

    def _conta(self, chave):
        self.server.contagem[chave] = self.server.contagem.get(chave, 0) + 1

    def do_GET(self):
        rota = urlsplit(self.path)
        parametros = parse_qs(rota.query)
        if rota.path == "/robots.txt":
            corpo = ("User-agent: *\nDisallow: /\n" if self.server.robots_bloqueia
                     else "User-agent: *\nDisallow:\n")
            self._responder(corpo, "text/plain")
        elif rota.path == "/jurisprudencia/":
            self._conta("home")
            self._responder("<html>portal</html>", "text/html; charset=utf-8",
                            [("Set-Cookie", "JSESSIONID=teste; Path=/")])
        elif rota.path == "/jurisprudencia/integra.do":
            rowid = parametros.get("rowid", [""])[0]
            self._conta(f"integra:{rowid}")
            if rowid == ID_A:
                self._responder(PDF_BYTES, "application/pdf")
            elif rowid == ID_B:
                self._responder('<html><iframe src="/arquivos/B.pdf"></iframe></html>',
                                "text/html; charset=utf-8")
            elif rowid == ID_C:
                self._responder('<html>visualizador <a href="/jurisprudencia/sobre.html">'
                                "ajuda</a> sem documento</html>", "text/html; charset=utf-8")
            elif rowid == ID_D:
                self._responder(PDF_BYTES, "text/html")  # CT errado de propósito
            else:
                self.send_error(404)
        elif rota.path == "/arquivos/B.pdf":
            self._conta("arquivo_b")
            self._responder(PDF_BYTES, "application/octet-stream")
        elif rota.path == "/jurisprudencia/html.do":
            doc = parametros.get("id", [""])[0]
            self._conta(f"htmldo:{doc}")
            self._responder(f"<html><body>Acordao {doc} em HTML (inteiro teor)</body></html>",
                            "text/html; charset=utf-8")
        else:
            self.send_error(404)

    def do_POST(self):
        rota = urlsplit(self.path)
        if rota.path != "/jurisprudencia/buscaajax.do":
            self.send_error(404)
            return
        self._conta("busca")
        tamanho = int(self.headers.get("Content-Length", "0"))
        # Como o portal real, decodifica o corpo do form em ISO-8859-1 e exige
        # o nome acentuado exato: se o scraper enviar UTF-8, "Cláudia" vira
        # "ClÃ¡udia" e a busca devolve zero resultados.
        corpo = parse_qs(self.rfile.read(tamanho).decode("ascii"), encoding="latin-1")
        pagina = int(corpo.get("page", ["1"])[0])
        eixo_frase = bool(corpo.get("frase", [""])[0])
        if (corpo.get("relator", [""])[0] != RELATORA
                or corpo.get("frase", [""])[0] not in ("", "tutela de urgência")):
            self._responder("<html>Considerando que o sistema não encontrou "
                            "resultados, analise os itens a seguir</html>",
                            "text/html; charset=utf-8")
            return
        if eixo_frase:
            # total declarado 60 com ps=50 -> o scraper deve pedir a página 2 e parar
            cabecalho = "<p>60 resultados encontrados</p>"
            blocos = (registro_html(NUM_A, ID_A) + registro_html(NUM_B, ID_B)
                      if pagina == 1 else registro_html(NUM_D, ID_D))
        else:
            cabecalho = "<p>2 resultados encontrados</p>"
            blocos = registro_html(NUM_A, ID_A) + registro_html(NUM_C, ID_C)
        self._responder(f"<html><body>{cabecalho}{blocos}</body></html>",
                        "text/html; charset=utf-8")


class Servidor(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.contagem = {}
        self.robots_bloqueia = False


class TesteScraper(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.servidor = Servidor(("127.0.0.1", 0), Portal)
        porta = cls.servidor.server_address[1]
        threading.Thread(target=cls.servidor.serve_forever, daemon=True).start()
        os.environ["TJSC_BASE"] = f"http://127.0.0.1:{porta}/jurisprudencia"
        os.environ["TJSC_MIN_INTERVALO"] = "0"
        os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        cls.scraper = importlib.import_module("scraper_tjsc")
        # Reduz o universo: 1 relatora, 2 eixos (suficiente p/ paginação e dedupe)
        cls.scraper.RELATORES = {"9a_camara": [RELATORA]}
        cls.scraper.EIXOS = {
            "prova_cognicao": {"frase": "tutela de urgência", "q": "alimentos prova",
                               "classe": "Agravo de Instrumento"},
            "binomio_renda": {"q": "alimentos possibilidade renda",
                              "classe": "Agravo de Instrumento"},
        }

    @classmethod
    def tearDownClass(cls):
        cls.servidor.shutdown()
        cls.servidor.server_close()

    def setUp(self):
        self.saida = tempfile.mkdtemp(prefix="tjsc_teste_")
        self.addCleanup(shutil.rmtree, self.saida, ignore_errors=True)
        self.servidor.contagem.clear()
        self.servidor.robots_bloqueia = False

    def _rodar(self, *flags):
        self.scraper.main(["--out", self.saida, *flags])

    def _csv(self):
        with open(os.path.join(self.saida, "index.csv"), newline="", encoding="utf-8") as arq:
            linhas = list(csv.DictReader(arq))
        return linhas, {linha["doc_id"]: linha for linha in linhas}

    def _downloads(self):
        """Contadores de requisições de download (ignora buscas/home/robots)."""
        return {chave: valor for chave, valor in self.servidor.contagem.items()
                if chave.startswith(("integra:", "htmldo:", "arquivo_b"))}

    def test_parser(self):
        html = ("<p>1.234 resultados encontrados</p>" + registro_html(NUM_A, ID_A)
                + registro_html(NUM_C, ID_C))
        registros, total = self.scraper.parse_resultados(html)
        self.assertEqual(total, 1234)
        self.assertEqual(len(registros), 2)
        primeiro = registros[0]
        self.assertEqual(primeiro["numero_processo"], NUM_A)
        self.assertEqual(primeiro["doc_id"], ID_A)
        self.assertEqual(primeiro["relator"], RELATORA)
        self.assertEqual(primeiro["orgao_julgador"], "Nona Câmara de Direito Civil")
        self.assertEqual(primeiro["comarca"], "Florianópolis")
        self.assertEqual(primeiro["data_julgamento"], "12/03/2025")
        self.assertEqual(primeiro["classe"], "Agravo de Instrumento")

    def test_fluxo_dry_run_download_e_idempotencia(self):
        # 1) dry-run: cataloga 4 documentos únicos (A deduplicado entre eixos), não baixa
        self._rodar("--dry-run")
        linhas, por_id = self._csv()
        self.assertEqual(len(linhas), 4)
        self.assertEqual(set(por_id), {ID_A, ID_B, ID_C, ID_D})
        self.assertEqual(por_id[ID_A]["eixo_origem"], "prova_cognicao")  # 1º eixo a encontrar
        self.assertEqual(por_id[ID_C]["eixo_origem"], "binomio_renda")
        self.assertTrue(all(linha["formato"] == "" and linha["arquivo"] == ""
                            for linha in linhas))
        self.assertEqual(self._downloads(), {}, "dry-run não pode baixar nada")
        raw = os.listdir(os.path.join(self.saida, "_raw_html"))
        self.assertIn("claudia_lambert_de_faria_prova_cognicao_p1.html", raw)
        self.assertIn("claudia_lambert_de_faria_prova_cognicao_p2.html", raw)  # paginação
        self.assertIn("claudia_lambert_de_faria_binomio_renda_p1.html", raw)
        self.assertTrue(os.path.exists(os.path.join(self.saida, "run.log")))

        # 2) execução real: baixa os 4 e reconcilia o index.csv (sem duplicar linhas)
        self._rodar()
        linhas, por_id = self._csv()
        self.assertEqual(len(linhas), 4)
        pasta = "9a_camara/claudia_lambert_de_faria"
        self.assertEqual(por_id[ID_A]["formato"], "pdf")
        self.assertEqual(por_id[ID_A]["arquivo"], f"{pasta}/{NUM_A}.pdf")
        self.assertEqual(por_id[ID_B]["formato"], "pdf", "deveria achar o PDF via iframe")
        self.assertEqual(por_id[ID_C]["formato"], "html", "sem PDF -> fallback html.do")
        self.assertTrue(por_id[ID_C]["arquivo"].endswith(f"{NUM_C}.html"))
        self.assertEqual(por_id[ID_D]["formato"], "pdf", "magic bytes %PDF com CT text/html")
        self.assertEqual(por_id[ID_D]["arquivo"], f"{pasta}/{NUM_A}_{ID_D[-8:]}.pdf",
                         "colisão de número de processo deve ganhar sufixo")
        for doc_id in (ID_A, ID_B, ID_D):
            caminho = os.path.join(self.saida, por_id[doc_id]["arquivo"])
            with open(caminho, "rb") as arq:
                self.assertTrue(arq.read().startswith(b"%PDF"), caminho)
        with open(os.path.join(self.saida, por_id[ID_C]["arquivo"]), encoding="utf-8") as arq:
            self.assertIn("inteiro teor", arq.read())
        downloads_apos_real = self._downloads()
        self.assertEqual(downloads_apos_real[f"integra:{ID_A}"], 1)

        # 3) reexecução: nada é rebaixado, catálogo não ganha linhas duplicadas
        self._rodar()
        linhas, _ = self._csv()
        self.assertEqual(len(linhas), 4)
        self.assertEqual(self._downloads(), downloads_apos_real,
                         "reexecução não pode repetir downloads")

    def test_robots_bloqueado_aborta(self):
        self.servidor.robots_bloqueia = True
        with self.assertRaises(SystemExit) as contexto:
            self._rodar("--dry-run")
        self.assertEqual(contexto.exception.code, 2)
        self.assertEqual(self.servidor.contagem.get("busca", 0), 0,
                         "nenhuma busca pode ocorrer com robots.txt proibitivo")


if __name__ == "__main__":
    unittest.main(verbosity=2)
