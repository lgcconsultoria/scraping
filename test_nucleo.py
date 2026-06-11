#!/usr/bin/env python3
"""Testes offline das utilidades do pacote nucleo/ (sem rede, sem API key).

Roda com: python test_nucleo.py -v

Cobre: ClienteHttp (rate-limit, backoff 429, backoff rede), texto_resposta,
slug/sanitizar_nome, carregar_index/regravar_index, reservar_base,
arquivo_existente, extrair_texto (html/rtf), sha256_texto/sha256_arquivo,
carregar_json/salvar_json, carregar_jsonl/compactar_jsonl, Contador/PRECOS_USD.
"""

import csv
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nucleo.http import ClienteHttp, texto_resposta
from nucleo.catalogo import (COLUNAS, slug, sanitizar_nome,
                              carregar_index, regravar_index,
                              reservar_base, arquivo_existente)
from nucleo.texto import extrair_texto, _rtf_minimo
from nucleo.cache import (sha256_texto, sha256_arquivo,
                           carregar_jsonl, compactar_jsonl,
                           carregar_json, salvar_json)
from nucleo.custos import Contador, PRECOS_USD


# ---------------------------------------------------------------------------
# Servidor HTTP mínimo para testar ClienteHttp
# ---------------------------------------------------------------------------

class ServidorSimples(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        comportamento = self.server.comportamento
        if comportamento == "ok":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"OK")
        elif comportamento == "ok_sem_charset":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"OK")
        elif comportamento == "429":
            self.server.contagem_429 += 1
            if self.server.contagem_429 < self.server.falhas_antes_ok:
                self.send_response(429)
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"OK")


class ServidorTeste(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.comportamento = "ok"
        self.contagem_429 = 0
        self.falhas_antes_ok = 2


# ---------------------------------------------------------------------------
# Testes: nucleo.http
# ---------------------------------------------------------------------------

class TesteHttp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.servidor = ServidorTeste(("127.0.0.1", 0), ServidorSimples)
        porta = cls.servidor.server_address[1]
        threading.Thread(target=cls.servidor.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{porta}"
        os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"

    @classmethod
    def tearDownClass(cls):
        cls.servidor.shutdown()
        cls.servidor.server_close()

    def _cliente(self, min_intervalo=0.0, max_tentativas=3):
        return ClienteHttp("TestAgent/1.0", min_intervalo=min_intervalo,
                           max_tentativas=max_tentativas)

    def test_get_simples(self):
        self.servidor.comportamento = "ok"
        resp = self._cliente().get(self.base + "/")
        self.assertEqual(resp.status_code, 200)

    def test_backoff_429_eventual_sucesso(self):
        self.servidor.comportamento = "429"
        self.servidor.contagem_429 = 0
        self.servidor.falhas_antes_ok = 2
        cliente = self._cliente(min_intervalo=0.0, max_tentativas=5)
        resp = cliente.get(self.base + "/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.servidor.contagem_429, 2)

    def test_backoff_429_esgota_tentativas(self):
        self.servidor.comportamento = "429"
        self.servidor.contagem_429 = 0
        self.servidor.falhas_antes_ok = 99
        cliente = self._cliente(min_intervalo=0.0, max_tentativas=2)
        with self.assertRaises(Exception):
            cliente.get(self.base + "/")

    def test_texto_resposta_com_charset(self):
        self.servidor.comportamento = "ok"
        resp = self._cliente().get(self.base + "/")
        texto = texto_resposta(resp)
        self.assertEqual(texto, "OK")

    def test_texto_resposta_sem_charset_usa_apparent(self):
        self.servidor.comportamento = "ok_sem_charset"
        resp = self._cliente().get(self.base + "/")
        texto = texto_resposta(resp)
        self.assertEqual(texto, "OK")

    def test_rate_limit_respeita_intervalo(self):
        self.servidor.comportamento = "ok"
        cliente = self._cliente(min_intervalo=0.05)
        t0 = time.monotonic()
        cliente.get(self.base + "/")
        cliente.get(self.base + "/")
        elapsed = time.monotonic() - t0
        self.assertGreaterEqual(elapsed, 0.05,
                                "segunda requisição deve aguardar o intervalo mínimo")


# ---------------------------------------------------------------------------
# Testes: nucleo.catalogo
# ---------------------------------------------------------------------------

class TesteCatalogo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nucleo_cat_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_slug_unicode(self):
        self.assertEqual(slug("Cláudia Lambert de Faria"),
                         "claudia_lambert_de_faria")
        self.assertEqual(slug("9ª Câmara"), "9a_camara")
        self.assertEqual(slug("  espaço  "), "espaco")

    def test_sanitizar_nome_caracteres_especiais(self):
        s = sanitizar_nome("Test/Case:Name<>")
        self.assertNotIn("/", s)
        self.assertNotIn(":", s)

    def test_sanitizar_nome_limite_150(self):
        longo = "a" * 200
        self.assertLessEqual(len(sanitizar_nome(longo)), 150)

    def test_colunas_completas(self):
        self.assertIn("doc_id", COLUNAS)
        self.assertIn("numero_processo", COLUNAS)
        self.assertIn("arquivo", COLUNAS)

    def test_carregar_index_vazio(self):
        caminho = os.path.join(self.tmp, "index.csv")
        linhas, por_id = carregar_index(caminho)
        self.assertEqual(linhas, [])
        self.assertEqual(por_id, {})

    def test_regravar_e_carregar_roundtrip(self):
        caminho = os.path.join(self.tmp, "index.csv")
        linha = {col: "" for col in COLUNAS}
        linha.update(doc_id="DOC1", numero_processo="1234", arquivo="a/b.pdf")
        regravar_index(caminho, [linha])
        linhas, por_id = carregar_index(caminho)
        self.assertEqual(len(linhas), 1)
        self.assertEqual(por_id["DOC1"]["numero_processo"], "1234")

    def test_reservar_base_sem_colisao(self):
        ocupados = {}
        b = reservar_base("9a_camara", "Cláudia Lambert de Faria", "DOC1", "1234-56", ocupados)
        self.assertEqual(b, "9a_camara/claudia_lambert_de_faria/1234-56")
        self.assertEqual(ocupados[b], "DOC1")

    def test_reservar_base_colisao_ganha_sufixo(self):
        ocupados = {}
        b1 = reservar_base("9a_camara", "Relator X", "DOC1", "NUM1", ocupados)
        b2 = reservar_base("9a_camara", "Relator X", "DOC2", "NUM1", ocupados)
        self.assertNotEqual(b2, b1, "colisão de nome deve gerar caminho diferente")
        self.assertTrue(b2.startswith(b1), "caminho com colisão deve ter o original como prefixo")

    def test_arquivo_existente_pdf(self):
        base = os.path.join(self.tmp, "doc")
        open(base + ".pdf", "w").close()
        arq, fmt = arquivo_existente(self.tmp, "doc")
        self.assertEqual(fmt, "pdf")
        self.assertTrue(arq.endswith(".pdf"))

    def test_arquivo_existente_rtf(self):
        base = os.path.join(self.tmp, "doc2")
        open(base + ".rtf", "w").close()
        _, fmt = arquivo_existente(self.tmp, "doc2")
        self.assertEqual(fmt, "rtf")

    def test_arquivo_existente_nenhum(self):
        arq, fmt = arquivo_existente(self.tmp, "inexistente")
        self.assertEqual(arq, "")
        self.assertEqual(fmt, "")


# ---------------------------------------------------------------------------
# Testes: nucleo.texto
# ---------------------------------------------------------------------------

class TesteTexto(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nucleo_txt_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_extrair_html_simples(self):
        caminho = os.path.join(self.tmp, "doc.html")
        with open(caminho, "w", encoding="utf-8") as f:
            f.write("<html><body><p>Alimentos ex-cônjuge</p></body></html>")
        texto = extrair_texto(caminho)
        self.assertIn("cônjuge", texto)

    def test_extrair_html_entidades(self):
        caminho = os.path.join(self.tmp, "doc2.html")
        with open(caminho, "w", encoding="utf-8") as f:
            f.write("<html><body>Aliment&aacute;rio &amp; Bin&ocirc;mio</body></html>")
        texto = extrair_texto(caminho)
        self.assertIn("&", texto)

    def test_rtf_minimo_decodifica(self):
        # RTF com sequência de escape cp1252: \'f4 = 'ô'
        rtf = r"{\rtf1 bin\'f4mio entre necessidade e possibilidade}"
        resultado = _rtf_minimo(rtf)
        self.assertIn("binômio", resultado.lower().replace("bin", "bin"))
        self.assertIn("necessidade", resultado)

    def test_extrair_rtf_via_arquivo(self):
        caminho = os.path.join(self.tmp, "doc.rtf")
        with open(caminho, "wb") as f:
            f.write(rb"{\rtf1\ansi Conteudo do acordao RTF}")
        texto = extrair_texto(caminho)
        self.assertIn("Conteudo", texto)

    def test_extensao_desconhecida_trata_como_html(self):
        caminho = os.path.join(self.tmp, "doc.txt")
        with open(caminho, "w", encoding="utf-8") as f:
            f.write("Texto simples sem tags")
        texto = extrair_texto(caminho)
        self.assertIn("Texto simples", texto)


# ---------------------------------------------------------------------------
# Testes: nucleo.cache
# ---------------------------------------------------------------------------

class TesteCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nucleo_cache_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_sha256_texto_deterministico(self):
        h1 = sha256_texto("hello")
        h2 = sha256_texto("hello")
        self.assertEqual(h1, h2)
        self.assertNotEqual(h1, sha256_texto("HELLO"))
        self.assertEqual(len(h1), 64)

    def test_sha256_arquivo_bate_com_conteudo(self):
        caminho = os.path.join(self.tmp, "arq.bin")
        with open(caminho, "wb") as f:
            f.write(b"dados de teste")
        h = sha256_arquivo(caminho)
        self.assertEqual(h, sha256_texto.__wrapped__("dados de teste")
                         if hasattr(sha256_texto, "__wrapped__")
                         else sha256_arquivo(caminho))
        self.assertEqual(len(h), 64)
        h_txt = sha256_texto("dados de teste")
        import hashlib
        esperado = hashlib.sha256(b"dados de teste").hexdigest()
        self.assertEqual(h, esperado)

    def test_json_roundtrip_atomico(self):
        caminho = os.path.join(self.tmp, "dados.json")
        dados = {"chave": "valor", "lista": [1, 2, 3]}
        salvar_json(caminho, dados)
        self.assertFalse(os.path.exists(caminho + ".tmp"),
                         "arquivo .tmp não deve sobrar após salvar_json")
        lido = carregar_json(caminho)
        self.assertEqual(lido, dados)

    def test_carregar_json_ausente_retorna_none(self):
        self.assertIsNone(carregar_json(os.path.join(self.tmp, "nao_existe.json")))

    def test_jsonl_roundtrip_ultimo_vence(self):
        caminho = os.path.join(self.tmp, "dados.jsonl")
        reg1 = {"doc_id": "D1", "valor": "v1"}
        reg2 = {"doc_id": "D1", "valor": "v2"}  # duplicata — deve vencer
        reg3 = {"doc_id": "D2", "valor": "v3"}
        with open(caminho, "w", encoding="utf-8") as f:
            for reg in (reg1, reg2, reg3):
                f.write(json.dumps(reg) + "\n")
        carregado = carregar_jsonl(caminho)
        self.assertEqual(len(carregado), 2)
        self.assertEqual(carregado["D1"]["valor"], "v2", "última ocorrência deve vencer")
        self.assertEqual(carregado["D2"]["valor"], "v3")

    def test_compactar_jsonl_remove_duplicatas(self):
        caminho = os.path.join(self.tmp, "dados2.jsonl")
        registros = {
            "D1": {"doc_id": "D1", "v": 1},
            "D2": {"doc_id": "D2", "v": 2},
        }
        compactar_jsonl(caminho, registros)
        self.assertFalse(os.path.exists(caminho + ".tmp"))
        lido = carregar_jsonl(caminho)
        self.assertEqual(set(lido), {"D1", "D2"})

    def test_jsonl_arquivo_ausente_retorna_dict_vazio(self):
        caminho = os.path.join(self.tmp, "nao_existe.jsonl")
        self.assertEqual(carregar_jsonl(caminho), {})


# ---------------------------------------------------------------------------
# Testes: nucleo.custos
# ---------------------------------------------------------------------------

class TesteContador(unittest.TestCase):
    def _uso(self, entrada=1000, saida=200, cache_criacao=300, cache_leitura=1500):
        class Uso:
            input_tokens = entrada
            output_tokens = saida
            cache_creation_input_tokens = cache_criacao
            cache_read_input_tokens = cache_leitura
        return Uso()

    def test_somar_acumula(self):
        c = Contador()
        c.somar("claude-opus-4-8", self._uso(entrada=100, saida=50,
                                              cache_criacao=0, cache_leitura=0))
        c.somar("claude-opus-4-8", self._uso(entrada=200, saida=50,
                                              cache_criacao=0, cache_leitura=0))
        self.assertEqual(c.por_modelo["claude-opus-4-8"]["entrada"], 300)
        self.assertEqual(c.por_modelo["claude-opus-4-8"]["saida"], 100)

    def test_custo_zero_sem_chamadas(self):
        c = Contador()
        self.assertEqual(c.custo(), 0.0)

    def test_custo_modelo_conhecido(self):
        c = Contador()
        # opus: entrada=$5/M, saída=$25/M
        # 1.000.000 tokens de entrada → $5.00 exato
        c.somar("claude-opus-4-8", self._uso(entrada=1_000_000, saida=0,
                                              cache_criacao=0, cache_leitura=0))
        self.assertAlmostEqual(c.custo(), 5.0, places=4)

    def test_custo_modelo_desconhecido_usa_fallback(self):
        c = Contador()
        c.somar("modelo-inexistente", self._uso(entrada=1_000_000, saida=0,
                                                 cache_criacao=0, cache_leitura=0))
        self.assertAlmostEqual(c.custo(), 5.0, places=4)

    def test_precos_usd_contem_modelos_esperados(self):
        for modelo in ("claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5"):
            self.assertIn(modelo, PRECOS_USD)

    def test_cache_leitura_mais_barata_que_entrada(self):
        c_normal = Contador()
        c_cache = Contador()
        c_normal.somar("claude-opus-4-8", self._uso(entrada=1_000_000, saida=0,
                                                      cache_criacao=0, cache_leitura=0))
        c_cache.somar("claude-opus-4-8", self._uso(entrada=0, saida=0,
                                                     cache_criacao=0, cache_leitura=1_000_000))
        self.assertLess(c_cache.custo(), c_normal.custo(),
                        "cache_leitura (0.1x) deve ser mais barata que entrada normal")


if __name__ == "__main__":
    unittest.main(verbosity=2)
