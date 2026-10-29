class LayoutAlterado(Exception):
    """A página respondeu 200, mas sem os dados esperados.

    Causas prováveis: o site mudou o formato do HTML ou devolveu uma página de bloqueio.
    """