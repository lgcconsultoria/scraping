"""Contratos JSON entre agentes: schemas leves de validação.

Documenta os campos esperados em Contexto.dados para cada tarefa e
fornece validação rápida antes de despachar para o agente.
"""
from __future__ import annotations

SCHEMAS: dict[str, dict] = {
    "pesquisa": {
        "dados_obrigatorios": [],
        "dados_opcionais": ["relator", "termos", "tribunais", "params", "max_decisoes"],
    },
    "redigir": {
        "dados_obrigatorios": ["tipo_peca", "fatos"],
        "dados_opcionais": ["decisoes", "pedidos", "requerente", "requerido"],
    },
    "revisar": {
        "dados_obrigatorios": ["rascunho"],
        "dados_opcionais": ["citacoes", "secoes", "score_qa"],
    },
}

TIPOS_PECA = ("tutela_antecipada", "peticao_alimentos")
TRIBUNAIS_SUPORTADOS = ("tjsc", "tjsp", "tjms")


def validar_contexto(tarefa: str, dados: dict) -> list[str]:
    """Retorna lista de erros de validação; lista vazia significa válido."""
    schema = SCHEMAS.get(tarefa)
    if schema is None:
        return [f"tarefa desconhecida: {tarefa!r} — opções: {', '.join(SCHEMAS)}"]

    erros = [
        f"campo obrigatório ausente: '{k}'"
        for k in schema["dados_obrigatorios"]
        if k not in dados
    ]

    if tarefa == "redigir" and "tipo_peca" in dados:
        if dados["tipo_peca"] not in TIPOS_PECA:
            erros.append(
                f"tipo_peca inválido: {dados['tipo_peca']!r} — opções: {', '.join(TIPOS_PECA)}"
            )

    if tarefa == "pesquisa" and "tribunais" in dados:
        invalidos = [t for t in dados["tribunais"] if t not in TRIBUNAIS_SUPORTADOS]
        if invalidos:
            erros.append(f"tribunais não suportados: {invalidos}")

    return erros
