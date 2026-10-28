import sqlite3
from model.client.protocol import Protocol
class ClientDatabase(Protocol):

    def __init__(self, caminho_banco: str):
        self.conexao = sqlite3.connect(caminho_banco)

    def executar_query(self, sql: str, params: tuple = ()):
        cursor = self.conexao.execute(sql, params)
        return cursor

