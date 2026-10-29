import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def ler_caminho_do_ambiente(nome_variavel):
    # Falha cedo e com mensagem clara, em vez de Path(None) estourar um TypeError confuso
    valor = os.getenv(nome_variavel)
    if valor is None:
        raise RuntimeError(f"Variável {nome_variavel} não está definida no .env")
    return Path(valor)