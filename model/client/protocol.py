from typing import Protocol, Any


class ClienteBanco(Protocol):

    def executar_query(
        self,
        sql: str,
        params: tuple = ()
    ) -> Any:
        ...