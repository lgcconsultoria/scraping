"""Rastreamento de uso de tokens e custo estimado em USD por modelo."""
import collections

PRECOS_USD = {
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}


class Contador:
    """Acumula uso de tokens por modelo e calcula o custo em dólar."""

    def __init__(self):
        self.por_modelo = collections.defaultdict(
            lambda: {"entrada": 0, "saida": 0, "cache_escrita": 0, "cache_leitura": 0})

    def somar(self, modelo, uso):
        registro = self.por_modelo[modelo]
        registro["entrada"] += uso.input_tokens
        registro["saida"] += uso.output_tokens
        registro["cache_escrita"] += getattr(uso, "cache_creation_input_tokens", 0) or 0
        registro["cache_leitura"] += getattr(uso, "cache_read_input_tokens", 0) or 0

    def custo(self):
        total = 0.0
        for modelo, u in self.por_modelo.items():
            entrada, saida = PRECOS_USD.get(modelo, (5.0, 25.0))
            total += (u["entrada"] * entrada + u["cache_escrita"] * entrada * 1.25
                      + u["cache_leitura"] * entrada * 0.1 + u["saida"] * saida) / 1_000_000
        return total
