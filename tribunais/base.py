"""Base abstrata para adaptadores de tribunal."""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterator


@dataclass
class InteiroTeor:
    caminho: str
    formato: str
    url: str


class AdaptadorTribunal(ABC):

    @abstractmethod
    def verificar_robots(self) -> bool:
        """Lê robots.txt; retorna True se as rotas necessárias são permitidas."""

    @abstractmethod
    def estabelecer_sessao(self) -> None:
        """Requisição inicial de sessão (cookies, handshake)."""

    @abstractmethod
    def parsear_resultados(self, html: str) -> "tuple[list[dict], int]":
        """Interpreta o HTML de uma página de resultados; retorna (registros, total)."""

    @abstractmethod
    def buscar(
        self,
        relator: str,
        params: dict,
        *,
        ps: int = 50,
    ) -> "Iterator[tuple[list[dict], int, int, str]]":
        """Gera (registros, total, pagina, html_bruto) paginando o portal."""

    @abstractmethod
    def baixar_inteiro_teor(
        self,
        doc_id: str,
        tipo: str,
        base_destino: str,
    ) -> InteiroTeor:
        """Baixa o inteiro teor; retorna InteiroTeor com caminho, formato e URL."""
