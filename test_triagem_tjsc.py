#!/usr/bin/env python3
"""Testes offline do triagem_tjsc (corpus sintético, sem rede).

Roda com: python test_triagem_tjsc.py -v
"""

import csv
import importlib
import os
import shutil
import sys
import tempfile
import unittest

# Conteúdos calibrados para scores exatos (ver dicionários em triagem_tjsc):
# DOC1 (html): eixo1 = ausencia de prova(+2) + mera alegacao(+2)
#              + necessidade de dilacao probatoria(+3) + indeferi(+1) = +8.
#              "indeferiu" NÃO pode disparar o termo contrário "deferi".
DOC1_HTML = ("<html><body><p>AGRAVO DE INSTRUMENTO. ALIMENTOS. Aus&ecirc;ncia de "
             "prova da altera&ccedil;&atilde;o financeira. Mera alega&ccedil;&atilde;o "
             "da parte. Necessidade de dila&ccedil;&atilde;o probat&oacute;ria "
             "evidente. O ju&iacute;zo de origem indeferiu a tutela.</p></body></html>")
# DOC2 (html): eixo2 = sinais exteriores de riqueza(-3) + presuncao de
#              capacidade(-2) = -5 -> CONTRARIA -> alerta.
DOC2_HTML = ("<html><body>APELA&Ccedil;&Atilde;O. Os sinais exteriores de riqueza "
             "autorizam a presun&ccedil;&atilde;o de capacidade do alimentante. "
             "Recurso provido.</body></html>")
# DOC3 (rtf): eixo2 = binomio(+3) + possibilidade do alimentante(+3)
#             + capacidade contributiva(+3) + renda liquida(+2) = +11.
DOC3_RTF = (rb"{\rtf1\ansi\ansicpg1252{\fonttbl{\f0 Arial;}}\f0 "
            rb"O bin\'f4mio entre necessidade e possibilidade do alimentante. "
            rb"Capacidade contributiva aferida pela renda l\'edquida do genitor.\par}")
DOC5_HTML = "<html><body></body></html>"          # sem texto -> precisa_ocr
DOC6_PDF = b"isto nao e um pdf de verdade"        # extracao falha -> erro:


class TesteTriagem(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        cls.triagem = importlib.import_module("triagem_tjsc")

    def setUp(self):
        self.saida = tempfile.mkdtemp(prefix="tjsc_triagem_")
        self.addCleanup(shutil.rmtree, self.saida, ignore_errors=True)
        os.makedirs(os.path.join(self.saida, "docs"))
        arquivos = {
            "docs/doc1.html": DOC1_HTML.encode("utf-8"),
            "docs/doc2.html": DOC2_HTML.encode("utf-8"),
            "docs/doc3.rtf": DOC3_RTF,
            "docs/doc5.html": DOC5_HTML.encode("utf-8"),
            "docs/doc6.pdf": DOC6_PDF,
        }
        for relativo, dados in arquivos.items():
            with open(os.path.join(self.saida, relativo), "wb") as arq:
                arq.write(dados)
        colunas = ["numero_processo", "relator", "camara", "orgao_julgador",
                   "comarca", "data_julgamento", "classe", "doc_id",
                   "eixo_origem", "formato", "arquivo", "url_integra"]

        def linha(num, arquivo, formato):
            return {"numero_processo": num, "relator": "Cláudia Lambert de Faria",
                    "camara": "9a_camara", "orgao_julgador": "Nona Câmara",
                    "comarca": "Capital", "data_julgamento": "01/02/2026",
                    "classe": "Agravo de Instrumento", "doc_id": num,
                    "eixo_origem": "prova_cognicao", "formato": formato,
                    "arquivo": arquivo, "url_integra": "http://exemplo/x"}

        with open(os.path.join(self.saida, "index.csv"), "w", newline="",
                  encoding="utf-8") as arq:
            escritor = csv.DictWriter(arq, fieldnames=colunas)
            escritor.writeheader()
            escritor.writerows([
                linha("DOC1", "docs/doc1.html", "html"),
                linha("DOC2", "docs/doc2.html", "html"),
                linha("DOC3", "docs/doc3.rtf", "rtf"),
                linha("DOC4", "docs/ausente.pdf", "pdf"),   # arquivo não existe
                linha("DOC5", "docs/doc5.html", "html"),
                linha("DOC6", "docs/doc6.pdf", "pdf"),
            ])

    def _csv(self):
        with open(os.path.join(self.saida, "triagem.csv"), newline="",
                  encoding="utf-8") as arq:
            return {r["numero_processo"]: r for r in csv.DictReader(arq)}

    def test_regex_fronteira_e_proximidade(self):
        norm = self.triagem.normalizar
        rx = self.triagem.termo_para_regex("deferi")
        self.assertIsNone(rx.search(norm("o juízo indeferiu a tutela")),
                          "'deferi' não pode disparar dentro de 'indeferiu'")
        self.assertIsNotNone(rx.search(norm("o juízo deferiu a tutela")))
        rx = self.triagem.termo_para_regex("provido o recurso")
        self.assertIsNone(rx.search(norm("desprovido o recurso")))
        self.assertIsNotNone(rx.search(norm("provido o recurso do autor")))
        rx = self.triagem.termo_para_regex("renda ... desconhecida")
        self.assertIsNotNone(rx.search(norm("renda do executado permanece desconhecida")))
        self.assertIsNone(rx.search(norm(
            "renda um dois tres quatro cinco seis sete oito desconhecida")))

    def test_normalizar_preserva_offsets(self):
        original = "Ausência de PROVA\n  no binômio"
        norm = self.triagem.normalizar(original)
        self.assertEqual(len(norm), len(original))
        m = self.triagem.termo_para_regex("ausencia de prova").search(norm)
        self.assertIsNotNone(m)
        self.assertEqual(original[m.start():m.end()], "Ausência de PROVA")

    def test_fluxo_completo(self):
        self.triagem.main(["--out", self.saida])
        linhas = self._csv()
        self.assertEqual(len(linhas), 6)
        doc1 = linhas["DOC1"]
        self.assertEqual(doc1["classe_eixo1"], "FAVORAVEL")
        self.assertEqual(doc1["score_eixo1"], "8",
                         "score errado: 'deferi' pode ter disparado em 'indeferiu'")
        self.assertEqual(doc1["classe_eixo2"], "NEUTRA")
        self.assertEqual(doc1["alerta_contrario"], "")
        doc2 = linhas["DOC2"]
        self.assertEqual(doc2["classe_eixo2"], "CONTRARIA")
        self.assertEqual(doc2["score_eixo2"], "-5")
        self.assertEqual(doc2["alerta_contrario"], "True")
        doc3 = linhas["DOC3"]
        self.assertEqual(doc3["classe_eixo2"], "FAVORAVEL")
        self.assertEqual(doc3["score_eixo2"], "11", "RTF deve ser extraído e pontuado")
        self.assertEqual(linhas["DOC4"]["precisa_ocr"], "arquivo_ausente")
        self.assertEqual(linhas["DOC5"]["precisa_ocr"], "True")
        self.assertTrue(linhas["DOC6"]["precisa_ocr"].startswith("erro:"))
        for pendente in ("DOC4", "DOC5", "DOC6"):
            self.assertEqual(linhas[pendente]["classe_eixo1"], "",
                             "sem texto não pode ser classificado como neutro")

        with open(os.path.join(self.saida, "relatorio_triagem.md"),
                  encoding="utf-8") as arq:
            md = arq.read()
        self.assertIn("ALERTA", md)
        self.assertLess(md.index("DOC2"), md.index("## Eixo 1"),
                        "documento contrário deve aparecer na seção de alerta, no topo")
        self.assertIn("sinais exteriores de riqueza", md)
        self.assertIn("binômio", md, "snippet deve vir do texto original, com acentos")
        for pendente in ("DOC4", "DOC5", "DOC6"):
            self.assertIn(pendente, md)

        # idempotência: reprocessar sobrescreve os derivados sem duplicar
        self.triagem.main(["--out", self.saida])
        self.assertEqual(len(self._csv()), 6)

    def test_index_ausente(self):
        vazio = tempfile.mkdtemp(prefix="tjsc_triagem_vazio_")
        self.addCleanup(shutil.rmtree, vazio, ignore_errors=True)
        with self.assertRaises(SystemExit):
            self.triagem.main(["--out", vazio])


if __name__ == "__main__":
    unittest.main(verbosity=2)
