"""Agente redator: gera rascunho de peça jurídica a partir de templates.

Tipos suportados: "tutela_antecipada", "peticao_alimentos".
Toda saída requer aprovação humana antes de uso em produção.

Entrada (Contexto.dados):
    {
        "tipo_peca":  str,
        "decisoes":   [dict],   # jurisprudência do pesquisador (até 3 citadas)
        "fatos":      str,
        "pedidos":    [str],
        "requerente": str,
        "requerido":  str,
    }

Saída (ResultadoAgente.saida, requer_aprovacao=True):
    {
        "rascunho":  str,
        "citacoes":  [[lei, artigo, texto], ...],
        "secoes":    [str],
    }
"""
from __future__ import annotations

from agentes.base import AgenteBase, Contexto, ResultadoAgente

_TEMPLATES: dict[str, str] = {
    "tutela_antecipada": (
        "EXCELENTÍSSIMO(A) SENHOR(A) DOUTOR(A) JUIZ(A) DE DIREITO\n\n"
        "{REQUERENTE}, já qualificado nos autos, vem respeitosamente perante V.Exa., "
        "com fundamento no art. 300 do Código de Processo Civil (Lei 13.105/2015), "
        "requerer a concessão de TUTELA ANTECIPADA DE URGÊNCIA, pelas razões a seguir.\n\n"
        "DOS FATOS\n\n"
        "{FATOS}\n\n"
        "DO DIREITO\n\n"
        "O art. 300 do CPC estabelece que a tutela de urgência será concedida quando houver "
        "elementos que evidenciem a probabilidade do direito e o perigo de dano ou risco ao "
        "resultado útil do processo. Presentes o fumus boni iuris e o periculum in mora.\n\n"
        "{JURISPRUDENCIA}"
        "DOS PEDIDOS\n\n"
        "Requer:\n{PEDIDOS}\n\n"
        "Nesses termos, pede deferimento.\n"
    ),
    "peticao_alimentos": (
        "EXCELENTÍSSIMO(A) SENHOR(A) DOUTOR(A) JUIZ(A) DE DIREITO DA VARA DE FAMÍLIA\n\n"
        "{REQUERENTE}, vem propor AÇÃO DE ALIMENTOS em face de {REQUERIDO}, "
        "com fundamento nos arts. 1.694 e 1.695 do Código Civil e na Lei n. 5.478/1968.\n\n"
        "DOS FATOS\n\n"
        "{FATOS}\n\n"
        "DO DIREITO\n\n"
        "Nos termos do art. 1.694 do Código Civil, podem os parentes pedir uns aos outros "
        "os alimentos de que necessitem para viver de modo compatível com a sua condição social. "
        "O binômio necessidade-possibilidade (art. 1.695 do CC) encontra-se demonstrado.\n\n"
        "{JURISPRUDENCIA}"
        "DOS PEDIDOS\n\n"
        "Requer:\n{PEDIDOS}\n\n"
        "Nesses termos, pede deferimento.\n"
    ),
}

_CITACOES: dict[str, list[list]] = {
    "tutela_antecipada": [
        ["CPC", "300",
         "A tutela de urgência será concedida quando houver elementos que evidenciem "
         "a probabilidade do direito e o perigo de dano ou o risco ao resultado útil "
         "do processo."],
    ],
    "peticao_alimentos": [
        ["CC", "1694",
         "Podem os parentes, os cônjuges ou companheiros pedir uns aos outros os alimentos "
         "de que necessitem para viver de modo compatível com a sua condição social, inclusive "
         "para atender às necessidades de sua educação."],
        ["CC", "1695",
         "São devidos os alimentos quando quem os pretende não tem bens suficientes, "
         "nem pode prover, pelo seu trabalho, à própria mantença, e aquele, de quem se "
         "reclamam, pode fornecê-los, sem desfalque do necessário ao seu sustento."],
    ],
}

_SECOES = ["DOS FATOS", "DO DIREITO", "DOS PEDIDOS"]


class AgenteRedator(AgenteBase):
    nome = "redator"

    def executar(self, ctx: Contexto) -> ResultadoAgente:
        dados = ctx.dados
        tipo = dados.get("tipo_peca", "tutela_antecipada")
        template = _TEMPLATES.get(tipo)
        if template is None:
            return self.erro(ctx.tarefa, f"tipo de peça desconhecido: {tipo}")

        decisoes = dados.get("decisoes", [])
        juri_linhas = []
        for d in decisoes[:3]:
            ementa = (d.get("ementa") or "")[:200]
            trib = (d.get("tribunal") or "").upper()
            if ementa:
                juri_linhas.append(f"- {trib}: {ementa}")
        jurisprudencia = (
            "Nesse sentido, a jurisprudência pátria:\n" + "\n".join(juri_linhas) + "\n\n"
            if juri_linhas else ""
        )

        pedidos_raw = dados.get("pedidos") or ["a concessão da medida requerida"]
        pedidos = "\n".join(f"{chr(97 + i)}) {p};" for i, p in enumerate(pedidos_raw))

        rascunho = template.format(
            REQUERENTE=dados.get("requerente") or "[REQUERENTE]",
            REQUERIDO=dados.get("requerido") or "[REQUERIDO]",
            FATOS=dados.get("fatos") or "[DESCREVER OS FATOS]",
            JURISPRUDENCIA=jurisprudencia,
            PEDIDOS=pedidos,
        )

        return self.ok(
            ctx.tarefa,
            {
                "rascunho":  rascunho,
                "citacoes":  _CITACOES.get(tipo, []),
                "secoes":    _SECOES,
            },
            requer_aprovacao=True,
        )
