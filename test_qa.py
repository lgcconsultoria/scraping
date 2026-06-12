"""Testes do gate anti-alucinação e da rubrica de qualidade (Phase 5)."""
import json
import os
import tempfile
import unittest
from datetime import date, timedelta

from qa.gate import (
    _normalizar,
    verificar_citacao,
    verificar_existencia,
    verificar_verbatim,
    verificar_vigencia,
)
from qa.rubrica import LIMIAR, avaliar_peca


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _lei_json(lei: str, artigos: dict, idade_dias: int = 0) -> dict:
    atualizado = (date.today() - timedelta(days=idade_dias)).isoformat()
    return {
        "lei": lei,
        "nome": f"Lei {lei}",
        "numero": "X.XXX",
        "ano": 2000,
        "url": "http://example.com",
        "hash": "abc123",
        "atualizado_em": atualizado,
        "vigente": True,
        "artigos": artigos,
    }


def _criar_cache(diretorio: str, leis: dict[str, dict]) -> None:
    os.makedirs(diretorio, exist_ok=True)
    for nome, dados in leis.items():
        with open(os.path.join(diretorio, f"{nome}.json"), "w", encoding="utf-8") as f:
            json.dump(dados, f, ensure_ascii=False)


_TEXTO_ART_300 = (
    "Art. 300. A tutela de urgência será concedida quando houver elementos que "
    "evidenciem a probabilidade do direito e o perigo de dano ou o risco ao "
    "resultado útil do processo."
)

_CITACAO_ART_300 = (
    "A tutela de urgência será concedida quando houver elementos que evidenciem "
    "a probabilidade do direito e o perigo de dano ou o risco ao resultado útil "
    "do processo."
)


# ---------------------------------------------------------------------------
# Normalização
# ---------------------------------------------------------------------------

class TesteNormalizacao(unittest.TestCase):
    def test_remove_acentos(self):
        self.assertEqual(_normalizar("urgência"), "urgencia")

    def test_minusculas(self):
        self.assertEqual(_normalizar("Alimentos"), "alimentos")

    def test_remove_pontuacao(self):
        resultado = _normalizar("Art. 300.")
        self.assertNotIn(".", resultado)

    def test_colapsa_espacos(self):
        self.assertEqual(_normalizar("a   b"), "a b")

    def test_texto_vazio(self):
        self.assertEqual(_normalizar(""), "")


# ---------------------------------------------------------------------------
# verificar_existencia
# ---------------------------------------------------------------------------

class TesteVerificarExistencia(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        _criar_cache(self.tmp, {
            "CPC": _lei_json("CPC", {"300": _TEXTO_ART_300}),
        })

    def test_artigo_existente(self):
        r = verificar_existencia("CPC", "300", self.tmp)
        self.assertTrue(r["ok"])
        self.assertEqual(r["check"], "existencia")
        self.assertEqual(r["msg"], "")

    def test_artigo_inexistente(self):
        r = verificar_existencia("CPC", "999", self.tmp)
        self.assertFalse(r["ok"])
        self.assertIn("999", r["msg"])

    def test_lei_nao_cacheada(self):
        r = verificar_existencia("CC", "1", self.tmp)
        self.assertFalse(r["ok"])

    def test_aceita_artigo_inteiro(self):
        r = verificar_existencia("CPC", 300, self.tmp)
        self.assertTrue(r["ok"])


# ---------------------------------------------------------------------------
# verificar_verbatim
# ---------------------------------------------------------------------------

class TesteVerificarVerbatim(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        _criar_cache(self.tmp, {
            "CPC": _lei_json("CPC", {"300": _TEXTO_ART_300}),
        })

    def test_citacao_identica_aprovada(self):
        r = verificar_verbatim("CPC", "300", _CITACAO_ART_300, 0.85, self.tmp)
        self.assertTrue(r["ok"])
        self.assertGreaterEqual(r["similaridade"], 0.85)

    def test_citacao_completamente_diferente_reprovada(self):
        r = verificar_verbatim(
            "CPC", "300",
            "Em matéria de precatório o devedor goza de prazo diferenciado.",
            0.85, self.tmp,
        )
        self.assertFalse(r["ok"])
        self.assertLess(r["similaridade"], 0.85)

    def test_artigo_inexistente_reprovado(self):
        r = verificar_verbatim("CPC", "999", _CITACAO_ART_300, 0.85, self.tmp)
        self.assertFalse(r["ok"])
        self.assertEqual(r["similaridade"], 0.0)

    def test_citacao_vazia_reprovada(self):
        r = verificar_verbatim("CPC", "300", "", 0.85, self.tmp)
        self.assertFalse(r["ok"])

    def test_limiar_zero_sempre_aprovado(self):
        r = verificar_verbatim("CPC", "300", "qualquer coisa", 0.0, self.tmp)
        self.assertTrue(r["ok"])

    def test_retorna_similaridade_numerica(self):
        r = verificar_verbatim("CPC", "300", _CITACAO_ART_300, 0.85, self.tmp)
        self.assertIsInstance(r["similaridade"], float)
        self.assertBetween(r["similaridade"], 0.0, 1.0)

    def assertBetween(self, value, low, high):
        self.assertGreaterEqual(value, low)
        self.assertLessEqual(value, high)


# ---------------------------------------------------------------------------
# verificar_vigencia
# ---------------------------------------------------------------------------

class TesteVerificarVigencia(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_cache_atual_aprovado(self):
        _criar_cache(self.tmp, {"CPC": _lei_json("CPC", {}, idade_dias=0)})
        r = verificar_vigencia("CPC", 30, self.tmp)
        self.assertTrue(r["ok"])
        self.assertEqual(r["idade_dias"], 0)

    def test_cache_velho_reprovado(self):
        _criar_cache(self.tmp, {"CPC": _lei_json("CPC", {}, idade_dias=31)})
        r = verificar_vigencia("CPC", 30, self.tmp)
        self.assertFalse(r["ok"])
        self.assertIn("31", r["msg"])

    def test_lei_ausente_reprovada(self):
        r = verificar_vigencia("CC", 30, self.tmp)
        self.assertFalse(r["ok"])
        self.assertIn("CC", r["msg"])

    def test_cache_limite_aprovado(self):
        _criar_cache(self.tmp, {"CPC": _lei_json("CPC", {}, idade_dias=30)})
        r = verificar_vigencia("CPC", 30, self.tmp)
        self.assertTrue(r["ok"])


# ---------------------------------------------------------------------------
# verificar_citacao (consolidado)
# ---------------------------------------------------------------------------

class TesteVerificarCitacao(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        _criar_cache(self.tmp, {
            "CPC": _lei_json("CPC", {"300": _TEXTO_ART_300}, idade_dias=0),
        })

    def test_todos_checks_passam(self):
        r = verificar_citacao("CPC", "300", _CITACAO_ART_300, diretorio=self.tmp)
        self.assertTrue(r["aprovado"])
        self.assertEqual(r["reprovados"], [])
        self.assertEqual(len(r["checks"]), 3)

    def test_artigo_inexistente_reprovado(self):
        r = verificar_citacao("CPC", "999", _CITACAO_ART_300, diretorio=self.tmp)
        self.assertFalse(r["aprovado"])
        self.assertIn("existencia", r["reprovados"])
        self.assertIn("verbatim", r["reprovados"])

    def test_cache_velho_reprovado(self):
        _criar_cache(self.tmp, {
            "CPC": _lei_json("CPC", {"300": _TEXTO_ART_300}, idade_dias=35),
        })
        r = verificar_citacao("CPC", "300", _CITACAO_ART_300, diretorio=self.tmp)
        self.assertFalse(r["aprovado"])
        self.assertIn("vigencia", r["reprovados"])


# ---------------------------------------------------------------------------
# avaliar_peca (rubrica)
# ---------------------------------------------------------------------------

_RASCUNHO_COMPLETO = (
    "DOS FATOS\n\nO autor requer alimentos e demonstra o fumus boni iuris.\n\n"
    "DO DIREITO\n\nTutela de urgência é cabível. Periculum in mora demonstrado.\n\n"
    "DOS PEDIDOS\n\na) concessão da tutela;\nb) citação do réu."
)


class TesteRubrica(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        _criar_cache(self.tmp, {
            "CPC": _lei_json("CPC", {"300": _TEXTO_ART_300}, idade_dias=0),
        })

    def test_peca_ideal_score_maximo(self):
        r = avaliar_peca(
            _RASCUNHO_COMPLETO,
            citacoes=[("CPC", "300", _CITACAO_ART_300)],
            fundamentos_esperados=["alimentos", "fumus boni iuris"],
            secoes_obrigatorias=["DOS FATOS", "DO DIREITO", "DOS PEDIDOS"],
            diretorio=self.tmp,
        )
        self.assertTrue(r["aprovada"])
        self.assertGreaterEqual(r["score"], LIMIAR)
        self.assertEqual(r["dimensoes"]["qualidade_formal"], 10.0)
        self.assertEqual(r["dimensoes"]["base_legal"], 10.0)

    def test_peca_sem_secoes_penaliza_formal(self):
        r = avaliar_peca(
            "Texto sem seções esperadas.",
            secoes_obrigatorias=["DOS FATOS", "DO DIREITO", "DOS PEDIDOS"],
            diretorio=self.tmp,
        )
        self.assertEqual(r["dimensoes"]["qualidade_formal"], 0.0)
        self.assertEqual(len(r["detalhes"]["secoes_ausentes"]), 3)

    def test_fundamento_ausente_penaliza_base_legal(self):
        r = avaliar_peca(
            "Texto sem o fundamento.",
            fundamentos_esperados=["alimentos gravídicos"],
            diretorio=self.tmp,
        )
        self.assertEqual(r["dimensoes"]["base_legal"], 0.0)
        self.assertIn("alimentos gravídicos", r["detalhes"]["fundamentos_ausentes"])

    def test_sem_citacoes_score_citacoes_maximo(self):
        r = avaliar_peca("Qualquer texto.", diretorio=self.tmp)
        self.assertEqual(r["dimensoes"]["citacoes_verificadas"], 10.0)

    def test_citacao_invalida_penaliza_score(self):
        r = avaliar_peca(
            _RASCUNHO_COMPLETO,
            citacoes=[("CPC", "999", "artigo inexistente")],
            diretorio=self.tmp,
        )
        self.assertEqual(r["dimensoes"]["citacoes_verificadas"], 0.0)

    def test_score_e_float_entre_0_e_10(self):
        r = avaliar_peca("texto", diretorio=self.tmp)
        self.assertGreaterEqual(r["score"], 0.0)
        self.assertLessEqual(r["score"], 10.0)

    def test_aprovacao_abaixo_limiar(self):
        r = avaliar_peca(
            "texto curto",
            citacoes=[("CPC", "999", "erro")],
            fundamentos_esperados=["fumus", "periculum"],
            secoes_obrigatorias=["DOS FATOS", "DO DIREITO", "DOS PEDIDOS"],
            diretorio=self.tmp,
        )
        self.assertFalse(r["aprovada"])

    def test_limiar_customizado(self):
        r = avaliar_peca(
            _RASCUNHO_COMPLETO,
            limiar=11.0,
            diretorio=self.tmp,
        )
        self.assertFalse(r["aprovada"])

    def test_detalhes_estrutura_completa(self):
        r = avaliar_peca("texto", diretorio=self.tmp)
        for chave in ("citacoes", "fundamentos_ausentes", "alertas_consistencia", "secoes_ausentes"):
            self.assertIn(chave, r["detalhes"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
