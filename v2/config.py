import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    URL_PORTAL: str = "https://novanfse.campinas.sp.gov.br/notafiscal/paginas/portal/index.html#/"

    # Certificado Digital — nome exato como aparece no seletor do Windows
    # Ex.: "NELSON LUIZ MAFRA JUNIOR:31929038801"
    CERT_NAME: str = field(default_factory=lambda: os.getenv("CERT_NAME", ""))

    PASTA_RESULTADOS: str = field(default_factory=lambda: os.getenv("PASTA_RESULTADOS", "resultados"))
    ARQUIVO_CLIENTES: str = field(default_factory=lambda: os.getenv("ARQUIVO_CLIENTES", "clientes.csv"))
    TIMEOUT_MS: int = field(default_factory=lambda: int(os.getenv("TIMEOUT_MS", "30000")))
