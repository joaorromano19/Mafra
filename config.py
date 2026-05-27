import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    URL_PORTAL: str = "https://novanfse.campinas.sp.gov.br/notafiscal/paginas/portal/index.html#/"
    CNPJ_PORTAL: str = field(default_factory=lambda: os.getenv("CNPJ_PORTAL", ""))
    SENHA_PORTAL: str = field(default_factory=lambda: os.getenv("SENHA_PORTAL", ""))
    PASTA_RESULTADOS: str = field(default_factory=lambda: os.getenv("PASTA_RESULTADOS", "resultados"))
    ARQUIVO_CLIENTES: str = field(default_factory=lambda: os.getenv("ARQUIVO_CLIENTES", "clientes.csv"))
    TIMEOUT_MS: int = field(default_factory=lambda: int(os.getenv("TIMEOUT_MS", "30000")))
    CAPSOLVER_API_KEY: str = field(default_factory=lambda: os.getenv("CAPSOLVER_API_KEY", ""))
