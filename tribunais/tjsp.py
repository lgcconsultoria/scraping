"""Adaptador para o portal eSAJ do TJSP (https://esaj.tjsp.jus.br/cjsg/)."""
from tribunais.esaj import AdaptadorEsaj


class AdaptadorTjsp(AdaptadorEsaj):
    BASE_PADRAO = "https://esaj.tjsp.jus.br"
    SIGLA = "tjsp"
