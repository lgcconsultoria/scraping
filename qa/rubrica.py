"""Rubrica de qualidade para peças jurídicas geradas por IA.

Pontuação 0–10 em quatro dimensões (ponderadas):
  citacoes_verificadas  (30%) — % de citações aprovadas pelo gate anti-alucinação
  base_legal            (30%) — cobertura dos fundamentos legais esperados no texto
  consistencia_factual  (20%) — ausência de contradições internas detectáveis
  qualidade_formal      (20%) — presença das seções obrigatórias da peça

Aprovação: score_total >= LIMIAR (padrão: 7.0).

Uso:
    from qa.rubrica import avaliar_peca

    resultado = avaliar_peca(
        texto_peca="...",
        citacoes=[("CPC", "300", "A tutela..."), ("CC", "1694", "Podem os parentes...")],
        fundamentos_esperados=["alimentos", "fumus boni iuris"],
        secoes_obrigatorias=["DOS FATOS", "DO DIREITO", "DOS PEDIDOS"],
    )
    if resultado["aprovada"]:
        ...
"""
from __future__ import annotations

import re

from qa.gate import verificar_citacao

LIMIAR: float = 7.0

PESOS: dict[str, float] = {
    "citacoes_verificadas": 0.30,
    "base_legal":           0.30,
    "consistencia_factual": 0.20,
    "qualidade_formal":     0.20,
}


# ------------------------------------------------------------------
# Dimensões individuais
# ------------------------------------------------------------------

def _pontuacao_citacoes(
    citacoes: list[tuple[str, str | int, str]],
    diretorio: str,
) -> tuple[float, list[dict]]:
    if not citacoes:
        return 10.0, []
    detalhes: list[dict] = []
    aprovadas = 0
    for lei, artigo, texto in citacoes:
        r = verificar_citacao(lei, artigo, texto, diretorio=diretorio)
        detalhes.append(r)
        if r["aprovado"]:
            aprovadas += 1
    score = 10.0 * aprovadas / len(citacoes)
    return round(score, 2), detalhes


def _pontuacao_base_legal(
    texto: str,
    fundamentos_esperados: list[str],
) -> tuple[float, list[str]]:
    if not fundamentos_esperados:
        return 10.0, []
    texto_lower = texto.lower()
    ausentes = [f for f in fundamentos_esperados if f.lower() not in texto_lower]
    cobertura = 1.0 - len(ausentes) / len(fundamentos_esperados)
    return round(10.0 * cobertura, 2), ausentes


def _pontuacao_consistencia(texto: str) -> tuple[float, list[str]]:
    """Heurística leve: detecta negações de palavras presentes na mesma frase."""
    alertas: list[str] = []
    neg_re = re.compile(r"(\b\w{4,}\b)[^.]{0,80}(?:não|jamais|nunca)\s+\1", re.I)
    for m in neg_re.finditer(texto):
        alertas.append(f"possível contradição: '{m.group()[:80]}'")
    score = max(0.0, 10.0 - 2.0 * len(alertas))
    return round(score, 2), alertas


def _pontuacao_formal(
    texto: str,
    secoes_obrigatorias: list[str],
) -> tuple[float, list[str]]:
    if not secoes_obrigatorias:
        return 10.0, []
    texto_upper = texto.upper()
    ausentes = [s for s in secoes_obrigatorias if s.upper() not in texto_upper]
    cobertura = 1.0 - len(ausentes) / len(secoes_obrigatorias)
    return round(10.0 * cobertura, 2), ausentes


# ------------------------------------------------------------------
# Avaliação consolidada
# ------------------------------------------------------------------

def avaliar_peca(
    texto_peca: str,
    *,
    citacoes: list[tuple[str, str | int, str]] | None = None,
    fundamentos_esperados: list[str] | None = None,
    secoes_obrigatorias: list[str] | None = None,
    limiar: float = LIMIAR,
    diretorio: str = "leis/dados",
) -> dict:
    """Avalia a peça jurídica e retorna resultado completo.

    Returns:
        {
            "aprovada": bool,
            "score": float,
            "limiar": float,
            "dimensoes": {citacoes_verificadas, base_legal, consistencia_factual, qualidade_formal},
            "detalhes": {citacoes, fundamentos_ausentes, alertas_consistencia, secoes_ausentes},
        }
    """
    s_cit, det_cit = _pontuacao_citacoes(citacoes or [], diretorio)
    s_leg, fund_ausen = _pontuacao_base_legal(texto_peca, fundamentos_esperados or [])
    s_con, alertas = _pontuacao_consistencia(texto_peca)
    s_for, sec_ausen = _pontuacao_formal(texto_peca, secoes_obrigatorias or [])

    dimensoes = {
        "citacoes_verificadas": s_cit,
        "base_legal":           s_leg,
        "consistencia_factual": s_con,
        "qualidade_formal":     s_for,
    }
    score = round(sum(dimensoes[k] * PESOS[k] for k in PESOS), 2)

    return {
        "aprovada": score >= limiar,
        "score": score,
        "limiar": limiar,
        "dimensoes": dimensoes,
        "detalhes": {
            "citacoes":               det_cit,
            "fundamentos_ausentes":   fund_ausen,
            "alertas_consistencia":   alertas,
            "secoes_ausentes":        sec_ausen,
        },
    }
