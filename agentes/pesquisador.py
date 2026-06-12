"""Agente pesquisador: busca jurisprudência nos tribunais configurados.

Entrada (Contexto.dados):
    {
        "relator":      str,          # filtra por nome do relator (opcional)
        "termos":       [str],        # palavras-chave para pós-filtro de ementa
        "tribunais":    ["tjsc", ...],
        "params":       {},           # parâmetros extras para o adaptador
        "max_decisoes": int,          # padrão: 50
    }

Saída (ResultadoAgente.saida):
    {
        "decisoes":              [dict],
        "total_encontrado":      int,
        "tribunais_consultados": [str],
        "erros":                 [str],  # presente apenas se houver falhas
    }
"""
from __future__ import annotations

import logging

from acervo.db import Acervo
from agentes.base import AgenteBase, Contexto, ResultadoAgente
from tribunais.tjms import AdaptadorTjms
from tribunais.tjsc import AdaptadorTjsc
from tribunais.tjsp import AdaptadorTjsp

_ADAPTADORES = {
    "tjsc": AdaptadorTjsc,
    "tjsp": AdaptadorTjsp,
    "tjms": AdaptadorTjms,
}

_USER_AGENT = (
    "LGCPesquisaJuridica/1.0 "
    "(pesquisador automatico; rate limit >= 2s; contato: lgclicitacao@gmail.com)"
)


class AgentePesquisador(AgenteBase):
    nome = "pesquisador"

    def __init__(self, db: Acervo, *, min_intervalo: float = 2.0):
        self.db = db
        self.min_intervalo = min_intervalo

    def executar(self, ctx: Contexto) -> ResultadoAgente:
        dados = ctx.dados
        relator = dados.get("relator", "")
        termos = [t.lower() for t in dados.get("termos", [])]
        tribunais_alvo = dados.get("tribunais", list(_ADAPTADORES))
        params = dados.get("params", {})
        max_decisoes = int(dados.get("max_decisoes", 50))

        decisoes: list[dict] = []
        consultados: list[str] = []
        erros: list[str] = []

        for sigla in tribunais_alvo:
            cls = _ADAPTADORES.get(sigla)
            if cls is None:
                erros.append(f"tribunal desconhecido: {sigla}")
                continue
            try:
                adaptador = cls(
                    user_agent=_USER_AGENT,
                    min_intervalo=self.min_intervalo,
                )
                if not adaptador.verificar_robots():
                    erros.append(f"{sigla}: bloqueado por robots.txt")
                    continue
                adaptador.estabelecer_sessao()
                for registros, _total, _pagina, _html in adaptador.buscar(relator, params):
                    for r in registros:
                        if termos and not any(
                            t in r.get("ementa", "").lower() for t in termos
                        ):
                            continue
                        r["tribunal"] = sigla
                        decisoes.append(r)
                        self.db.inserir(r)
                        if len(decisoes) >= max_decisoes:
                            break
                    if len(decisoes) >= max_decisoes:
                        break
                consultados.append(sigla)
            except Exception as exc:
                logging.warning("erro consultando %s: %s", sigla, exc)
                erros.append(f"{sigla}: {exc}")

        saida: dict = {
            "decisoes":              decisoes,
            "total_encontrado":      len(decisoes),
            "tribunais_consultados": consultados,
        }
        if erros:
            saida["erros"] = erros
        return self.ok(ctx.tarefa, saida)
