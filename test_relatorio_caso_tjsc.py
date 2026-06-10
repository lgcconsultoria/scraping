#!/usr/bin/env python3
"""Testes offline do relatorio_caso_tjsc contra um stub da API da Anthropic.

Roda com: python test_relatorio_caso_tjsc.py -v

O stub local implementa POST /v1/messages com respostas no formato real da
Messages API: para chamadas de análise (com output_config) devolve o JSON
estruturado; para a síntese devolve o relatório em markdown. Nenhuma chamada
sai para a internet e nenhuma chave real é usada.
"""

import csv
import importlib
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

NUM1 = "5000001-11.2025.8.24.0000"
NUM2 = "5000002-22.2025.8.24.0000"

TEXTO_DOC = ("<html><body>AGRAVO DE INSTRUMENTO. ALIMENTOS ENTRE EX-CÔNJUGES. "
             "AUSÊNCIA DE PROVA DA NECESSIDADE. FATURAMENTO DA EMPRESA QUE NÃO SE "
             "CONFUNDE COM A RENDA DO SÓCIO. BINÔMIO NECESSIDADE-POSSIBILIDADE. "
             "DECISÃO REFORMADA. RECURSO PROVIDO." + " lorem juris" * 30 + "</body></html>")

ANALISE_FAVORAVEL = {
    "posicao": "favoravel", "aplicabilidade": 9,
    "teses_apoiadas": ["faturamento não é renda do sócio"],
    "resumo_relevancia": "Acórdão reformou fixação baseada em faturamento.",
    "trechos_citaveis": [{"trecho": "FATURAMENTO DA EMPRESA QUE NÃO SE CONFUNDE",
                          "como_usar": "Citar no tópico do binômio."}],
    "ressalvas": "",
}
ANALISE_CONTRARIA = {
    "posicao": "contrario", "aplicabilidade": 3,
    "teses_apoiadas": ["sinais exteriores de riqueza"],
    "resumo_relevancia": "Acórdão presumiu capacidade por sinais exteriores.",
    "trechos_citaveis": [],
    "ressalvas": "Caso com filhos menores; distinguível.",
}
RELATORIO_STUB = ("## RELATÓRIO SINTÉTICO TESTE\n\nA defesa está amparada na tese do "
                  f"binômio (TJSC, AI n. {NUM1}).")


class StubAnthropic(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        if self.path.rstrip("/") != "/v1/messages":
            self.send_error(404)
            return
        tamanho = int(self.headers.get("Content-Length", "0"))
        corpo = json.loads(self.rfile.read(tamanho).decode("utf-8"))
        if "output_config" in corpo:
            self.server.contagem["analises"] += 1
            pergunta = corpo["messages"][0]["content"]
            analise = ANALISE_FAVORAVEL if NUM1 in pergunta else ANALISE_CONTRARIA
            texto = json.dumps(analise, ensure_ascii=False)
        else:
            self.server.contagem["sinteses"] += 1
            texto = RELATORIO_STUB
        resposta = {
            "id": "msg_teste", "type": "message", "role": "assistant",
            "model": corpo.get("model", "claude-opus-4-8"),
            "content": [{"type": "text", "text": texto}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 1000, "output_tokens": 200,
                      "cache_creation_input_tokens": 500,
                      "cache_read_input_tokens": 2000},
        }
        dados = json.dumps(resposta).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(dados)))
        self.end_headers()
        self.wfile.write(dados)


class Servidor(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.contagem = {"analises": 0, "sinteses": 0}


class TesteRelatorioCaso(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.servidor = Servidor(("127.0.0.1", 0), StubAnthropic)
        porta = cls.servidor.server_address[1]
        threading.Thread(target=cls.servidor.serve_forever, daemon=True).start()
        os.environ["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{porta}"
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-chave-de-teste"
        os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        cls.modulo = importlib.import_module("relatorio_caso_tjsc")

    @classmethod
    def tearDownClass(cls):
        cls.servidor.shutdown()
        cls.servidor.server_close()

    def setUp(self):
        self.saida = tempfile.mkdtemp(prefix="tjsc_caso_")
        self.addCleanup(shutil.rmtree, self.saida, ignore_errors=True)
        self.servidor.contagem.update(analises=0, sinteses=0)
        os.makedirs(os.path.join(self.saida, "docs"))
        for nome in ("doc1", "doc2"):
            with open(os.path.join(self.saida, "docs", f"{nome}.html"), "w",
                      encoding="utf-8") as arq:
                arq.write(TEXTO_DOC)
        colunas = ["numero_processo", "relator", "camara", "orgao_julgador", "comarca",
                   "data_julgamento", "classe", "doc_id", "eixo_origem", "formato",
                   "arquivo", "url_integra"]

        def linha(num, doc_id, arquivo):
            return {"numero_processo": num, "relator": "Des. Teste",
                    "camara": "9a_camara", "orgao_julgador": "Nona Câmara",
                    "comarca": "Capital", "data_julgamento": "01/02/2026",
                    "classe": "Agravo de Instrumento", "doc_id": doc_id,
                    "eixo_origem": "binomio_renda", "formato": "html",
                    "arquivo": arquivo, "url_integra": "http://exemplo/x"}

        with open(os.path.join(self.saida, "index.csv"), "w", newline="",
                  encoding="utf-8") as arq:
            escritor = csv.DictWriter(arq, fieldnames=colunas)
            escritor.writeheader()
            escritor.writerows([
                linha(NUM1, "D1" * 15, "docs/doc1.html"),
                linha(NUM2, "D2" * 15, "docs/doc2.html"),
                linha("9999999-99.2025.8.24.0000", "D3" * 15, ""),  # sem arquivo
            ])
        self.caso = os.path.join(self.saida, "caso.md")
        with open(self.caso, "w", encoding="utf-8") as arq:
            arq.write("AGRAVO DE INSTRUMENTO. Tese: faturamento bruto não é renda. "
                      "Tese: ausência de prova da necessidade." * 20)

    def _rodar(self, *flags):
        self.modulo.main(["--caso", self.caso, "--out", self.saida, *flags])

    def _analises(self):
        with open(os.path.join(self.saida, "analises_caso.jsonl"),
                  encoding="utf-8") as arq:
            return [json.loads(l) for l in arq if l.strip()]

    def test_fluxo_completo_e_retomada(self):
        self._rodar()
        registros = self._analises()
        self.assertEqual(len(registros), 2, "doc sem arquivo não pode ser analisado")
        por_numero = {r["numero_processo"]: r for r in registros}
        self.assertEqual(por_numero[NUM1]["analise"]["posicao"], "favoravel")
        self.assertEqual(por_numero[NUM2]["analise"]["posicao"], "contrario")
        self.assertEqual(self.servidor.contagem, {"analises": 2, "sinteses": 1})

        with open(os.path.join(self.saida, "relatorio_caso.md"), encoding="utf-8") as arq:
            md = arq.read()
        self.assertIn("RELATÓRIO SINTÉTICO TESTE", md)
        self.assertIn("Apêndice", md)
        self.assertIn(NUM1, md)
        self.assertIn(NUM2, md)
        self.assertIn("favoravel", md)

        # retomada: análises vêm do cache, só a síntese é refeita (1 chamada nova)
        self._rodar()
        self.assertEqual(self.servidor.contagem, {"analises": 2, "sinteses": 2})
        self.assertEqual(len(self._analises()), 2)

    def test_limite(self):
        self._rodar("--limite", "1")
        self.assertEqual(self.servidor.contagem["analises"], 1)
        self.assertEqual(len(self._analises()), 1)

    def test_exige_chave(self):
        chave = os.environ.pop("ANTHROPIC_API_KEY")
        try:
            with self.assertRaises(SystemExit):
                self._rodar()
        finally:
            os.environ["ANTHROPIC_API_KEY"] = chave

    def test_recorte_preserva_inicio_e_fim(self):
        texto = "EMENTA " + ("miolo " * 20000) + "DISPOSITIVO"
        recortado = self.modulo.recortar(texto, teto=1000)
        self.assertLess(len(recortado), 1200)
        self.assertTrue(recortado.startswith("EMENTA"))
        self.assertTrue(recortado.endswith("DISPOSITIVO"))
        self.assertIn("omitido", recortado)


if __name__ == "__main__":
    unittest.main(verbosity=2)
