"""Contrato base para agentes da plataforma LGC.

Todos os agentes trocam mensagens via dicts JSON encapsulados em
Contexto (entrada) e ResultadoAgente (saída).
"""
from __future__ import annotations

import dataclasses
import json
from abc import ABC, abstractmethod
from typing import Any


@dataclasses.dataclass
class Contexto:
    """Envelope de entrada para qualquer agente."""
    tarefa: str
    dados: dict[str, Any]
    historico: list[dict] = dataclasses.field(default_factory=list)
    meta: dict[str, Any] = dataclasses.field(default_factory=dict)

    def serializar(self) -> str:
        return json.dumps(dataclasses.asdict(self), ensure_ascii=False, indent=2)

    @classmethod
    def desserializar(cls, texto: str) -> "Contexto":
        return cls(**json.loads(texto))


@dataclasses.dataclass
class ResultadoAgente:
    """Envelope de saída de qualquer agente."""
    agente: str
    tarefa: str
    status: str                # "ok" | "erro" | "aguardando_aprovacao"
    saida: dict[str, Any]
    mensagem: str = ""
    requer_aprovacao: bool = False

    def serializar(self) -> str:
        return json.dumps(dataclasses.asdict(self), ensure_ascii=False, indent=2)

    @classmethod
    def desserializar(cls, texto: str) -> "ResultadoAgente":
        return cls(**json.loads(texto))


class AgenteBase(ABC):
    """Interface base: define contrato e helpers de resultado."""

    nome: str = "agente_base"

    @abstractmethod
    def executar(self, ctx: Contexto) -> ResultadoAgente:
        """Executa a tarefa descrita em ctx e retorna o resultado."""

    def ok(
        self,
        tarefa: str,
        saida: dict,
        *,
        requer_aprovacao: bool = False,
    ) -> ResultadoAgente:
        return ResultadoAgente(
            agente=self.nome,
            tarefa=tarefa,
            status="aguardando_aprovacao" if requer_aprovacao else "ok",
            saida=saida,
            requer_aprovacao=requer_aprovacao,
        )

    def erro(self, tarefa: str, mensagem: str) -> ResultadoAgente:
        return ResultadoAgente(
            agente=self.nome,
            tarefa=tarefa,
            status="erro",
            saida={},
            mensagem=mensagem,
        )
