"""Testes do repositório híbrido de decisões (Phase 4 — acervo/db.py)."""
import json
import os
import tempfile
import unittest

from acervo.db import Acervo


def _decisao(n: int, tribunal: str = "tjsc") -> dict:
    return {
        "doc_id":          f"doc-{n}",
        "tribunal":        tribunal,
        "relator":         f"Des. Relator {n}",
        "tipo":            "Acórdão",
        "data_julgamento": f"2024-0{n}-01",
        "ementa":          f"Alimentos gravídicos. Tutela de urgência. Decisão {n}.",
        "url":             f"https://example.com/{n}",
    }


class TesteAcervoBasico(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = Acervo(os.path.join(self.tmp, "test.db"))

    def tearDown(self):
        self.db.close()

    def test_inserir_nova_decisao_retorna_true(self):
        self.assertTrue(self.db.inserir(_decisao(1)))

    def test_inserir_duplicata_retorna_false(self):
        self.db.inserir(_decisao(1))
        self.assertFalse(self.db.inserir(_decisao(1)))

    def test_obter_por_doc_id(self):
        self.db.inserir(_decisao(2))
        reg = self.db.obter("doc-2")
        self.assertIsNotNone(reg)
        self.assertEqual(reg["relator"], "Des. Relator 2")
        self.assertEqual(reg["tribunal"], "tjsc")

    def test_obter_inexistente_retorna_none(self):
        self.assertIsNone(self.db.obter("nao-existe"))

    def test_campos_opcionais_podem_ser_omitidos(self):
        self.db.inserir({"doc_id": "minimal"})
        reg = self.db.obter("minimal")
        self.assertIsNotNone(reg)
        self.assertEqual(reg["tribunal"], "")
        self.assertEqual(reg["ementa"], "")

    def test_excluir_existente_retorna_true(self):
        self.db.inserir(_decisao(1))
        self.assertTrue(self.db.excluir("doc-1"))

    def test_excluir_inexistente_retorna_false(self):
        self.assertFalse(self.db.excluir("fantasma"))

    def test_context_manager_fecha_conexao(self):
        db_path = os.path.join(self.tmp, "ctx.db")
        with Acervo(db_path) as db:
            db.inserir(_decisao(1))
            reg = db.obter("doc-1")
        self.assertIsNotNone(reg)


class TesteAcervoBusca(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = Acervo(os.path.join(self.tmp, "test.db"))
        for i in range(1, 4):
            self.db.inserir(_decisao(i, tribunal="tjsc" if i < 3 else "tjsp"))

    def tearDown(self):
        self.db.close()

    def test_busca_fts_encontra_termo(self):
        resultados = self.db.buscar_texto("alimentos")
        self.assertEqual(len(resultados), 3)

    def test_busca_fts_nao_encontra_termo_ausente(self):
        self.assertEqual(self.db.buscar_texto("precatório"), [])

    def test_busca_filtros_por_tribunal(self):
        resultados = self.db.buscar_filtros(tribunal="tjsp")
        self.assertEqual(len(resultados), 1)
        self.assertEqual(resultados[0]["tribunal"], "tjsp")

    def test_busca_filtros_por_relator_parcial(self):
        resultados = self.db.buscar_filtros(relator="Relator 1")
        self.assertEqual(len(resultados), 1)

    def test_busca_filtros_por_data(self):
        resultados = self.db.buscar_filtros(data_inicio="2024-02-01")
        self.assertEqual(len(resultados), 2)

    def test_busca_filtros_sem_resultados(self):
        resultados = self.db.buscar_filtros(tribunal="tjrj")
        self.assertEqual(resultados, [])


class TesteAcervoDownloads(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = Acervo(os.path.join(self.tmp, "test.db"))
        self.db.inserir(_decisao(1))

    def tearDown(self):
        self.db.close()

    def test_registrar_download(self):
        self.db.registrar_download("doc-1", "/tmp/doc.pdf", "pdf", "sha256abc")
        dls = self.db.downloads("doc-1")
        self.assertEqual(len(dls), 1)
        self.assertEqual(dls[0]["formato"], "pdf")
        self.assertEqual(dls[0]["hash_arquivo"], "sha256abc")

    def test_downloads_vazios_sem_registro(self):
        self.assertEqual(self.db.downloads("doc-1"), [])

    def test_upsert_download_mesmo_caminho(self):
        self.db.registrar_download("doc-1", "/tmp/doc.pdf", "pdf", "hash1")
        self.db.registrar_download("doc-1", "/tmp/doc.pdf", "pdf", "hash2")
        dls = self.db.downloads("doc-1")
        self.assertEqual(len(dls), 1)
        self.assertEqual(dls[0]["hash_arquivo"], "hash2")


class TesteAcervoStats(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = Acervo(os.path.join(self.tmp, "test.db"))
        self.db.inserir(_decisao(1, "tjsc"))
        self.db.inserir(_decisao(2, "tjsp"))
        self.db.inserir(_decisao(3, "tjsc"))
        self.db.registrar_download("doc-1", "/tmp/a.pdf", "pdf")

    def tearDown(self):
        self.db.close()

    def test_stats_total(self):
        s = self.db.stats()
        self.assertEqual(s["total"], 3)

    def test_stats_por_tribunal(self):
        s = self.db.stats()
        self.assertEqual(s["por_tribunal"]["tjsc"], 2)
        self.assertEqual(s["por_tribunal"]["tjsp"], 1)

    def test_stats_downloads(self):
        self.assertEqual(self.db.stats()["downloads"], 1)

    def test_exportar_jsonl(self):
        caminho = os.path.join(self.tmp, "export.jsonl")
        n = self.db.exportar_jsonl(caminho)
        self.assertEqual(n, 3)
        self.assertTrue(os.path.exists(caminho))
        with open(caminho, encoding="utf-8") as f:
            linhas = [l for l in f if l.strip()]
        self.assertEqual(len(linhas), 3)
        self.assertIn("doc_id", json.loads(linhas[0]))

    def test_exportar_jsonl_com_filtro(self):
        caminho = os.path.join(self.tmp, "tjsc.jsonl")
        n = self.db.exportar_jsonl(caminho, tribunal="tjsc")
        self.assertEqual(n, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
