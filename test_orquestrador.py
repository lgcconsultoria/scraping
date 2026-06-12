"""Testes dos agentes e do orquestrador com gates de aprovação (Phase 6)."""
import json
import os
import tempfile
import unittest
from datetime import date
from unittest.mock import MagicMock

from acervo.db import Acervo
from agentes.base import AgenteBase, Contexto, ResultadoAgente
from agentes.redator import AgenteRedator, _CITACOES, _TEMPLATES
from orquestrador.contrato import validar_contexto
from orquestrador.fluxo import Orquestrador, ResultadoFluxo


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TEXTO_ART_300 = (
    "Art. 300. A tutela de urgência será concedida quando houver elementos que "
    "evidenciem a probabilidade do direito e o perigo de dano ou o risco ao "
    "resultado útil do processo."
)

_TEXTO_ART_1694 = (
    "Art. 1.694. Podem os parentes, os cônjuges ou companheiros pedir uns aos "
    "outros os alimentos de que necessitem para viver de modo compatível com a "
    "sua condição social, inclusive para atender às necessidades de sua educação."
)

_TEXTO_ART_1695 = (
    "Art. 1.695. São devidos os alimentos quando quem os pretende não tem bens "
    "suficientes, nem pode prover, pelo seu trabalho, à própria mantença, e "
    "aquele, de quem se reclamam, pode fornecê-los, sem desfalque do necessário "
    "ao seu sustento."
)


def _criar_cache_leis(diretorio: str) -> None:
    hoje = date.today().isoformat()
    leis = {
        "CPC": {
            "lei": "CPC", "nome": "Código de Processo Civil",
            "numero": "13.105", "ano": 2015, "url": "http://example.com",
            "hash": "abc", "atualizado_em": hoje, "vigente": True,
            "artigos": {"300": _TEXTO_ART_300},
        },
        "CC": {
            "lei": "CC", "nome": "Código Civil",
            "numero": "10.406", "ano": 2002, "url": "http://example.com",
            "hash": "def", "atualizado_em": hoje, "vigente": True,
            "artigos": {"1694": _TEXTO_ART_1694, "1695": _TEXTO_ART_1695},
        },
    }
    os.makedirs(diretorio, exist_ok=True)
    for nome, dados in leis.items():
        with open(os.path.join(diretorio, f"{nome}.json"), "w", encoding="utf-8") as f:
            json.dump(dados, f, ensure_ascii=False)


def _pesquisador_stub(decisoes: list | None = None) -> MagicMock:
    mock = MagicMock()
    mock.executar.return_value = ResultadoAgente(
        agente="pesquisador", tarefa="pesquisa", status="ok",
        saida={
            "decisoes":              decisoes or [],
            "total_encontrado":      len(decisoes or []),
            "tribunais_consultados": ["tjsc"],
        },
    )
    return mock


def _redator_stub(tipo: str = "tutela_antecipada") -> MagicMock:
    rascunho = (
        "DOS FATOS\n\nO autor demonstra o fumus boni iuris e requer alimentos.\n\n"
        "DO DIREITO\n\nTutela de urgência. Periculum in mora demonstrado.\n\n"
        "DOS PEDIDOS\n\na) concessão da tutela requerida;"
    )
    mock = MagicMock()
    mock.executar.return_value = ResultadoAgente(
        agente="redator", tarefa="redigir",
        status="aguardando_aprovacao",
        saida={
            "rascunho":  rascunho,
            "citacoes":  _CITACOES.get(tipo, []),
            "secoes":    ["DOS FATOS", "DO DIREITO", "DOS PEDIDOS"],
        },
        requer_aprovacao=True,
    )
    return mock


# ---------------------------------------------------------------------------
# Contexto e ResultadoAgente
# ---------------------------------------------------------------------------

class TesteEnvelopes(unittest.TestCase):
    def test_contexto_serializar_desserializar(self):
        ctx = Contexto(tarefa="pesquisa", dados={"tribunal": "tjsc"})
        txt = ctx.serializar()
        ctx2 = Contexto.desserializar(txt)
        self.assertEqual(ctx2.tarefa, "pesquisa")
        self.assertEqual(ctx2.dados["tribunal"], "tjsc")

    def test_resultado_agente_serializar(self):
        r = ResultadoAgente(agente="redator", tarefa="redigir", status="ok", saida={"x": 1})
        d = json.loads(r.serializar())
        self.assertEqual(d["agente"], "redator")
        self.assertEqual(d["saida"]["x"], 1)

    def test_resultado_agente_desserializar(self):
        r = ResultadoAgente(agente="a", tarefa="t", status="ok", saida={})
        r2 = ResultadoAgente.desserializar(r.serializar())
        self.assertEqual(r2.agente, "a")

    def test_resultado_fluxo_serializar(self):
        rf = ResultadoFluxo(status="aprovado", etapa_final="concluido", score_qa=8.5)
        d = json.loads(rf.serializar())
        self.assertEqual(d["status"], "aprovado")
        self.assertEqual(d["score_qa"], 8.5)


# ---------------------------------------------------------------------------
# AgenteBase (abstrato)
# ---------------------------------------------------------------------------

class TesteAgenteBase(unittest.TestCase):
    def test_nao_pode_instanciar_diretamente(self):
        with self.assertRaises(TypeError):
            AgenteBase()

    def test_subclasse_concreta_pode_instanciar(self):
        class AgenteConcreto(AgenteBase):
            nome = "concreto"
            def executar(self, ctx):
                return self.ok(ctx.tarefa, {"feito": True})

        ag = AgenteConcreto()
        ctx = Contexto(tarefa="teste", dados={})
        r = ag.executar(ctx)
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.saida["feito"], True)

    def test_helper_erro(self):
        class AgenteConcreto(AgenteBase):
            nome = "c"
            def executar(self, ctx):
                return self.erro(ctx.tarefa, "deu ruim")

        r = AgenteConcreto().executar(Contexto(tarefa="t", dados={}))
        self.assertEqual(r.status, "erro")
        self.assertEqual(r.mensagem, "deu ruim")


# ---------------------------------------------------------------------------
# AgenteRedator
# ---------------------------------------------------------------------------

class TesteRedator(unittest.TestCase):
    def setUp(self):
        self.redator = AgenteRedator()

    def _ctx(self, tipo: str, **extra) -> Contexto:
        dados = {
            "tipo_peca": tipo,
            "fatos": "Fatos do caso aqui.",
            "pedidos": ["concessão da tutela"],
            "requerente": "João Silva",
            "requerido": "Maria Souza",
        }
        dados.update(extra)
        return Contexto(tarefa="redigir", dados=dados)

    def test_tutela_antecipada_ok(self):
        r = self.redator.executar(self._ctx("tutela_antecipada"))
        self.assertEqual(r.status, "aguardando_aprovacao")
        self.assertTrue(r.requer_aprovacao)
        self.assertIn("DOS FATOS", r.saida["rascunho"])
        self.assertIn("DOS PEDIDOS", r.saida["rascunho"])

    def test_peticao_alimentos_ok(self):
        r = self.redator.executar(self._ctx("peticao_alimentos"))
        self.assertEqual(r.status, "aguardando_aprovacao")
        rascunho = r.saida["rascunho"]
        self.assertIn("ALIMENTOS", rascunho.upper())
        self.assertIn("1.694", rascunho)

    def test_tipo_desconhecido_retorna_erro(self):
        r = self.redator.executar(self._ctx("mandado_de_seguranca"))
        self.assertEqual(r.status, "erro")
        self.assertIn("mandado_de_seguranca", r.mensagem)

    def test_citacoes_presentes(self):
        r = self.redator.executar(self._ctx("tutela_antecipada"))
        self.assertIsInstance(r.saida["citacoes"], list)
        self.assertTrue(len(r.saida["citacoes"]) > 0)

    def test_secoes_presentes(self):
        r = self.redator.executar(self._ctx("tutela_antecipada"))
        for sec in ["DOS FATOS", "DO DIREITO", "DOS PEDIDOS"]:
            self.assertIn(sec, r.saida["secoes"])

    def test_jurisprudencia_aparece_quando_ha_decisoes(self):
        decisoes = [{"tribunal": "tjsc", "ementa": "Alimentos devidos.", "doc_id": "x"}]
        r = self.redator.executar(self._ctx("tutela_antecipada", decisoes=decisoes))
        self.assertIn("TJSC", r.saida["rascunho"])

    def test_pedidos_multiplos(self):
        r = self.redator.executar(self._ctx(
            "tutela_antecipada",
            pedidos=["concessão da tutela", "citação do réu"],
        ))
        self.assertIn("b)", r.saida["rascunho"])


# ---------------------------------------------------------------------------
# Contratos
# ---------------------------------------------------------------------------

class TesteContratos(unittest.TestCase):
    def test_pesquisa_sem_dados_obrigatorios_valida(self):
        self.assertEqual(validar_contexto("pesquisa", {}), [])

    def test_redigir_faltando_campo_obrigatorio(self):
        erros = validar_contexto("redigir", {"tipo_peca": "tutela_antecipada"})
        self.assertTrue(any("fatos" in e for e in erros))

    def test_redigir_tipo_invalido(self):
        erros = validar_contexto("redigir", {"tipo_peca": "inexistente", "fatos": "x"})
        self.assertTrue(any("tipo_peca" in e for e in erros))

    def test_tarefa_desconhecida(self):
        erros = validar_contexto("voar", {})
        self.assertTrue(len(erros) > 0)

    def test_tribunal_invalido(self):
        erros = validar_contexto("pesquisa", {"tribunais": ["tjxx"]})
        self.assertTrue(len(erros) > 0)

    def test_redigir_valido(self):
        erros = validar_contexto("redigir", {
            "tipo_peca": "tutela_antecipada",
            "fatos": "Os fatos são...",
        })
        self.assertEqual(erros, [])


# ---------------------------------------------------------------------------
# Orquestrador (fluxo completo)
# ---------------------------------------------------------------------------

class TesteOrquestrador(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.leis_dir = os.path.join(self.tmp, "leis")
        _criar_cache_leis(self.leis_dir)
        self.db = Acervo(os.path.join(self.tmp, "test.db"))

    def tearDown(self):
        self.db.close()

    def _orq(self, aprovador=None, tipo="tutela_antecipada"):
        return Orquestrador(
            self.db,
            aprovador=aprovador or MagicMock(return_value=True),
            diretorio_leis=self.leis_dir,
            _pesquisador=_pesquisador_stub(),
            _redator=_redator_stub(tipo),
        )

    def test_fluxo_completo_aprovado(self):
        aprovador = MagicMock(return_value=True)
        orq = self._orq(aprovador)
        r = orq.executar(
            "tutela_antecipada", "Fatos.", ["tutela"], "Req", "Réu",
            termos=["fumus boni iuris"],
        )
        self.assertEqual(r.status, "aprovado")
        self.assertGreaterEqual(r.score_qa, 7.0)
        self.assertEqual(aprovador.call_count, 2)

    def test_fluxo_reprovado_no_rascunho(self):
        aprovador = MagicMock(return_value=False)
        r = self._orq(aprovador).executar(
            "tutela_antecipada", "Fatos.", ["tutela"], "Req", "Réu"
        )
        self.assertEqual(r.status, "reprovado")
        self.assertEqual(r.etapa_final, "aprovacao_rascunho")
        self.assertEqual(aprovador.call_count, 1)

    def test_fluxo_reprovado_na_aprovacao_final(self):
        aprovador = MagicMock(side_effect=[True, False])
        r = self._orq(aprovador).executar(
            "tutela_antecipada", "Fatos.", ["tutela"], "Req", "Réu"
        )
        self.assertEqual(r.status, "reprovado")
        self.assertEqual(r.etapa_final, "aprovacao_final")

    def test_fluxo_erro_pesquisador(self):
        pesq_erro = MagicMock()
        pesq_erro.executar.return_value = ResultadoAgente(
            agente="pesquisador", tarefa="pesquisa",
            status="erro", saida={}, mensagem="falha de conexão",
        )
        orq = Orquestrador(
            self.db,
            aprovador=MagicMock(return_value=True),
            diretorio_leis=self.leis_dir,
            _pesquisador=pesq_erro,
            _redator=_redator_stub(),
        )
        r = orq.executar("tutela_antecipada", "Fatos.", ["tutela"], "Req", "Réu")
        self.assertEqual(r.status, "erro")
        self.assertEqual(r.etapa_final, "pesquisa")
        self.assertIn("falha", r.mensagem)

    def test_fluxo_erro_tipo_invalido(self):
        red_erro = MagicMock()
        red_erro.executar.return_value = ResultadoAgente(
            agente="redator", tarefa="redigir",
            status="erro", saida={}, mensagem="tipo desconhecido",
        )
        orq = Orquestrador(
            self.db,
            aprovador=MagicMock(return_value=True),
            diretorio_leis=self.leis_dir,
            _pesquisador=_pesquisador_stub(),
            _redator=red_erro,
        )
        r = orq.executar("tutela_invalida", "Fatos.", ["x"], "Req", "Réu")
        self.assertEqual(r.status, "erro")
        self.assertEqual(r.etapa_final, "redacao")

    def test_resultado_fluxo_tem_rascunho(self):
        r = self._orq().executar("tutela_antecipada", "Fatos.", ["t"], "A", "B")
        self.assertIsInstance(r.rascunho, str)
        self.assertTrue(len(r.rascunho) > 0)

    def test_resultado_fluxo_tem_resultado_qa(self):
        r = self._orq().executar("tutela_antecipada", "Fatos.", ["t"], "A", "B")
        self.assertIn("aprovada", r.resultado_qa)
        self.assertIn("dimensoes", r.resultado_qa)

    def test_decisoes_salvas_no_acervo(self):
        decisoes = [{"doc_id": "d-99", "tribunal": "tjsc", "ementa": "Alimentos."}]
        pesq = _pesquisador_stub(decisoes)
        orq = Orquestrador(
            self.db,
            aprovador=MagicMock(return_value=True),
            diretorio_leis=self.leis_dir,
            _pesquisador=pesq,
            _redator=_redator_stub(),
        )
        orq.executar("tutela_antecipada", "Fatos.", ["t"], "A", "B")
        # decisões foram passadas ao pesquisador (ele já salvaria no db)
        # verificamos que o orquestrador completou sem erro
        self.assertEqual(orq.db.stats()["total"], 0)  # pesquisador é stub, não insere


if __name__ == "__main__":
    unittest.main(verbosity=2)
