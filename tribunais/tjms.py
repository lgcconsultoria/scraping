"""Adaptador para o portal eSAJ do TJMS (https://esaj.tjms.jus.br/cjsg/)."""
from tribunais.esaj import AdaptadorEsaj


class AdaptadorTjms(AdaptadorEsaj):
    BASE_PADRAO = "https://esaj.tjms.jus.br"
    SIGLA = "tjms"
