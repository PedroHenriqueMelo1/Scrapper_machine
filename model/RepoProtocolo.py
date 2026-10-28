from typing import Protocol
from model.client.protocol import ClienteBanco

class ProtocolRepositorio(Protocol):
    def __init__(self, client: ClienteBanco) -> None:
        ...

