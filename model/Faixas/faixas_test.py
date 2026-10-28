import sqlite3

from model.Faixas.Repositorio_Faixas import Repositorio_Faixas
from model.client.ClienteBanco import ClientDatabase


def test_adicionar_faixa():
    # Arrange: prepara o banco de teste
    cliente = ClientDatabase(":memory:")

    cliente.executar_query("""
        CREATE TABLE faixas_preco (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            preco_minimo INTEGER NOT NULL,
            preco_maximo INTEGER NOT NULL,
            segmento_valor TEXT,
            numero_paginas INTEGER DEFAULT 0,
            finalizado INTEGER DEFAULT 0,
            motivo_finalizacao TEXT,
            imoveis_novos INTEGER DEFAULT 0,
            observacao TEXT,
            data_criacao TEXT,
            data_atualizacao TEXT,
            UNIQUE (preco_minimo, preco_maximo)
        )
    """)

    repositorio = Repositorio_Faixas(cliente)

    # Act: executa o método que queremos testar
    faixa_id = repositorio.adicionar_faixa(100_000, 105_000)

    # Assert: verifica se a faixa foi cadastrada
    assert faixa_id == 1

    resultado = cliente.executar_query(
        "SELECT preco_minimo, preco_maximo FROM faixas_preco WHERE id = ?",
        (faixa_id,),
    ).fetchone()

    assert resultado == (100_000, 105_000)

    cliente.conexao.close()