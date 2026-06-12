"""Orquestrador do fluxo completo com gates de aprovação humana.

Fluxo:
  1. AgentePesquisador  — busca jurisprudência relevante
  2. AgenteRedator      — gera rascunho (→ gate humano)
  3. qa.gate + rubrica  — valida citações e avalia qualidade
  4. Gate humano final  — revisão do operador antes da entrega

Qualquer etapa pode retornar status "reprovado" ou "erro" e encerrar o fluxo.
Score mínimo de QA: 7.0 (configurável via limiar_qa).
"""
from __future__ import annotations

import dataclasses
import json
import logging
from typing import Callable

from acervo.db import Acervo
from agentes.base import Contexto, ResultadoAgente
from agentes.pesquisador import AgentePesquisador
from agentes.redator import AgenteRedator
from qa.rubrica import avaliar_peca

AprovadorFn = Callable[[str, dict], bool]


def _aprovador_terminal(etapa: str, dados: dict) -> bool:
    """Aprovador interativo via stdin. Substitua por stub em testes."""
    print(f"\n{'='*60}")
    print(f"APROVAÇÃO NECESSÁRIA — {etapa}")
    print(json.dumps(dados, ensure_ascii=False, indent=2)[:800])
    print("=" * 60)
    resposta = input("Aprovar? [s/N]: ").strip().lower()
    return resposta in ("s", "sim", "y", "yes")


@dataclasses.dataclass
class ResultadoFluxo:
    """Resultado consolidado do fluxo completo."""
    status: str           # "aprovado" | "reprovado" | "erro"
    etapa_final: str      # última etapa executada
    rascunho: str = ""
    score_qa: float = 0.0
    resultado_qa: dict = dataclasses.field(default_factory=dict)
    mensagem: str = ""

    def serializar(self) -> str:
        return json.dumps(dataclasses.asdict(self), ensure_ascii=False, indent=2)


class Orquestrador:
    """Coordena agentes e gates de aprovação humana."""

    def __init__(
        self,
        db: Acervo,
        *,
        aprovador: AprovadorFn = _aprovador_terminal,
        diretorio_leis: str = "leis/dados",
        min_intervalo: float = 2.0,
        _pesquisador=None,
        _redator=None,
    ):
        self.db = db
        self.aprovador = aprovador
        self.diretorio_leis = diretorio_leis
        self.pesquisador = _pesquisador or AgentePesquisador(db, min_intervalo=min_intervalo)
        self.redator = _redator or AgenteRedator()

    def executar(
        self,
        tipo_peca: str,
        fatos: str,
        pedidos: list[str],
        requerente: str,
        requerido: str,
        *,
        termos: list[str] | None = None,
        tribunais: list[str] | None = None,
        relator: str = "",
        limiar_qa: float = 7.0,
    ) -> ResultadoFluxo:
        """Executa o fluxo completo e retorna o resultado consolidado."""

        # ── 1. Pesquisa ───────────────────────────────────────────────
        logging.info("Etapa 1: pesquisa de jurisprudência")
        ctx_pesq = Contexto(
            tarefa="pesquisa",
            dados={
                "relator":      relator,
                "termos":       termos or [],
                "tribunais":    tribunais or ["tjsc"],
                "max_decisoes": 10,
            },
        )
        res_pesq: ResultadoAgente = self.pesquisador.executar(ctx_pesq)
        if res_pesq.status == "erro":
            return ResultadoFluxo(
                status="erro",
                etapa_final="pesquisa",
                mensagem=res_pesq.mensagem,
            )
        decisoes = res_pesq.saida.get("decisoes", [])
        logging.info("Pesquisa: %d decisões encontradas", len(decisoes))

        # ── 2. Redação ────────────────────────────────────────────────
        logging.info("Etapa 2: redação do rascunho")
        ctx_red = Contexto(
            tarefa="redigir",
            dados={
                "tipo_peca":  tipo_peca,
                "decisoes":   decisoes,
                "fatos":      fatos,
                "pedidos":    pedidos,
                "requerente": requerente,
                "requerido":  requerido,
            },
        )
        res_red: ResultadoAgente = self.redator.executar(ctx_red)
        if res_red.status == "erro":
            return ResultadoFluxo(
                status="erro",
                etapa_final="redacao",
                mensagem=res_red.mensagem,
            )

        rascunho = res_red.saida.get("rascunho", "")
        citacoes_raw = res_red.saida.get("citacoes", [])

        # ── Gate humano: aprovação do rascunho ────────────────────────
        if res_red.requer_aprovacao:
            if not self.aprovador(
                "rascunho gerado pelo redator",
                {"tipo_peca": tipo_peca, "preview": rascunho[:500]},
            ):
                return ResultadoFluxo(
                    status="reprovado",
                    etapa_final="aprovacao_rascunho",
                    rascunho=rascunho,
                    mensagem="operador recusou o rascunho",
                )

        # ── 3. QA ─────────────────────────────────────────────────────
        logging.info("Etapa 3: avaliação de qualidade")
        secoes = res_red.saida.get("secoes", [])
        citacoes = [tuple(c) for c in citacoes_raw if len(c) == 3]
        resultado_qa = avaliar_peca(
            rascunho,
            citacoes=citacoes,
            fundamentos_esperados=termos or [],
            secoes_obrigatorias=secoes,
            diretorio=self.diretorio_leis,
        )
        score = resultado_qa["score"]
        logging.info("QA score: %.1f (limiar: %.1f)", score, limiar_qa)

        if score < limiar_qa:
            return ResultadoFluxo(
                status="reprovado",
                etapa_final="qa",
                rascunho=rascunho,
                score_qa=score,
                resultado_qa=resultado_qa,
                mensagem=f"QA reprovado (score={score:.1f} < {limiar_qa:.1f})",
            )

        # ── Gate humano: aprovação final ──────────────────────────────
        if not self.aprovador(
            "peça aprovada no QA — revisão final do operador",
            {"score_qa": score, "preview": rascunho[:500]},
        ):
            return ResultadoFluxo(
                status="reprovado",
                etapa_final="aprovacao_final",
                rascunho=rascunho,
                score_qa=score,
                resultado_qa=resultado_qa,
                mensagem="operador recusou na revisão final",
            )

        return ResultadoFluxo(
            status="aprovado",
            etapa_final="concluido",
            rascunho=rascunho,
            score_qa=score,
            resultado_qa=resultado_qa,
        )
