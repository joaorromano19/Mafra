"""
Agente NFSe Campinas — Versão 2 (Certificado Digital)
======================================================
Login via certificado digital A1/A3 instalado no repositório do Windows.
O Chrome instalado é usado para mTLS (o Chromium embutido do Playwright
não tem acesso ao CNG/CAPI do Windows).
"""

import asyncio
import calendar
import json
import logging
import os
import re
from pathlib import Path
from datetime import datetime

from playwright.async_api import async_playwright, Page, BrowserContext

from config import Config

logger = logging.getLogger(__name__)


PORTAL_URL = "https://novanfse.campinas.sp.gov.br/notafiscal/paginas/portal/index.html#/"


class ClienteNaoEncontradoError(Exception):
    pass


_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


class NFSeAgente:
    def __init__(self, config: Config):
        self.config = config
        self._playwright = None
        self._browser = None
        self._context: BrowserContext = None
        self._page: Page = None

    # ------------------------------------------------------------------ setup

    async def iniciar(self):
        """Inicia o Chrome instalado com auto-seleção de certificado.

        O certificado digital exige:
          - Chrome real (não Chromium do Playwright) → acesso ao repositório
            de certificados do Windows (CNG/CAPI).
          - Modo visível → mTLS exige uma sessão de browser válida.

        A flag --auto-select-certificate-for-urls instrui o Chrome a
        selecionar automaticamente o certificado correto sem mostrar o
        dialog do Windows. Filtro por CN (subject common name).
        """
        self._playwright = await async_playwright().start()

        # Auto-seleciona o certificado para o portal NFSe sem mostrar dialog.
        # O filtro é por CN (Common Name) do subject do certificado.
        auto_select = json.dumps([{
            "pattern": "https://novanfse.campinas.sp.gov.br",
            "filter": {
                "SUBJECT": {
                    "CN": self.config.CERT_NAME
                }
            }
        }])

        self._browser = await self._playwright.chromium.launch(
            headless=False,
            channel="chrome",
            args=[
                "--no-sandbox",
                "--ignore-certificate-errors",
                "--disable-blink-features=AutomationControlled",
                f"--auto-select-certificate-for-urls={auto_select}",
            ],
        )
        self._context = await self._browser.new_context(
            accept_downloads=True,
            ignore_https_errors=True,
            viewport={"width": 1280, "height": 800},
            user_agent=_USER_AGENT,
        )
        self._page = await self._context.new_page()
        self._page.set_default_timeout(self.config.TIMEOUT_MS)
        logger.info(
            f"Chrome iniciado. Auto-seleção de certificado: {self.config.CERT_NAME}"
        )

    async def fechar(self):
        try:
            if self._context:
                await self._context.close()
        except Exception:
            pass
        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass
        if self._playwright:
            await self._playwright.stop()
        logger.info("Browser encerrado.")

    # ------------------------------------------------------------------ login

    async def login(self):
        """Faz login no portal usando certificado digital.

        Fluxo real do portal (duas páginas distintas):
          1. Navega para `index.html#/` (home com botão AUTENTICAR no menu)
          2. Clica em AUTENTICAR → SPA navega para `index.html#/login`
          3. Na tela `#/login`, clica em UTILIZAR CERTIFICADO
          4. Chrome auto-seleciona o certificado (sem dialog)
          5. Portal redireciona para `bemVindo.jsf` autenticado
        """
        logger.info("Acessando portal NFSe Campinas...")
        await self._page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=30_000)

        # Espera o botão AUTENTICAR aparecer (sinal de que o SPA renderizou).
        await self._page.wait_for_selector(
            "button:has-text('AUTENTICAR'), "
            "a:has-text('AUTENTICAR'), "
            "[class*='btn']:has-text('AUTENTICAR')",
            state="visible",
            timeout=20_000,
        )

        # Fecha overlays iniciais (comunicados, cookies)
        await self._page.keyboard.press("Escape")
        await asyncio.sleep(0.3)

        # Passo 1: clicar em AUTENTICAR (botão azul no menu superior direito)
        await self._clicar_autenticar()

        # Aguarda navegar para a tela de login (#/login)
        try:
            await self._page.wait_for_url("**/#/login**", timeout=15_000)
        except Exception:
            pass  # URL pode não disparar — o próximo wait do botão valida

        # Passo 2: clicar em UTILIZAR CERTIFICADO na tela de login
        await self._clicar_certificado_digital()

        # Passo 3: aguardar autenticação por mTLS — o Chrome seleciona o
        # certificado automaticamente via --auto-select-certificate-for-urls
        await self._aguardar_login_certificado(timeout=60)

        logger.info(f"Login realizado. URL: {self._page.url}")

    async def _clicar_autenticar(self):
        """Clica no botão AUTENTICAR (canto superior direito da home).

        Cascata de seletores em sequência — loga qual venceu.
        """
        # Estratégia 1: Playwright locator com :has-text
        for seletor in [
            "button:has-text('AUTENTICAR')",
            "a:has-text('AUTENTICAR')",
            "[class*='btn']:has-text('AUTENTICAR')",
        ]:
            try:
                btn = self._page.locator(seletor).first
                await btn.wait_for(state="visible", timeout=5_000)
                await btn.dispatch_event("click")
                logger.info(f"Clicou em AUTENTICAR via '{seletor}'.")
                return
            except Exception:
                pass

        # Estratégia 2: JS click direto
        clicou = await self._page.evaluate("""
            () => {
                const btns = [...document.querySelectorAll('button, a, [role="button"]')];
                const btn = btns.find(b =>
                    (b.textContent || '').trim().toUpperCase().includes('AUTENTICAR')
                );
                if (btn) { btn.click(); return true; }
                return false;
            }
        """)
        if clicou:
            logger.info("Clicou em AUTENTICAR via JS.")
            return

        raise RuntimeError("Botão AUTENTICAR não encontrado na página.")

    async def _clicar_certificado_digital(self):
        """Clica no botão UTILIZAR CERTIFICADO na tela de login (#/login).

        Cascata de seletores em sequência.
        """
        for seletor in [
            "button:has-text('UTILIZAR CERTIFICADO')",
            "a:has-text('UTILIZAR CERTIFICADO')",
            "button:has-text('Certificado')",
            "a:has-text('Certificado')",
            "text=UTILIZAR CERTIFICADO",
        ]:
            try:
                btn = self._page.locator(seletor).first
                await btn.wait_for(state="visible", timeout=5_000)
                await btn.dispatch_event("click")
                logger.info(f"Clicou em UTILIZAR CERTIFICADO via '{seletor}'.")
                return
            except Exception:
                pass

        # Fallback: JS click
        clicou = await self._page.evaluate("""
            () => {
                const btns = [...document.querySelectorAll('button, a, [role="button"]')];
                const btn = btns.find(b => {
                    const txt = (b.textContent || '').trim().toUpperCase();
                    return txt.includes('UTILIZAR CERTIFICADO') || txt.includes('CERTIFICADO');
                });
                if (btn) { btn.click(); return true; }
                return false;
            }
        """)
        if clicou:
            logger.info("Clicou em UTILIZAR CERTIFICADO via JS.")
            return

        raise RuntimeError("Botão UTILIZAR CERTIFICADO não encontrado na tela de login.")

    async def _aguardar_login_certificado(self, timeout: int = 60):
        """Aguarda o Chrome completar a autenticação mTLS e o portal redirecionar."""
        logger.info("Aguardando autenticação por certificado digital...")
        try:
            await self._page.wait_for_url("**/*.jsf**", timeout=timeout * 1_000)
        except Exception:
            url_atual = self._page.url
            raise RuntimeError(
                f"Login por certificado não redirecionou para .jsf após {timeout}s. "
                f"URL atual: {url_atual}. "
                "Verifique se o certificado digital está instalado e selecionado."
            )
        logger.info(f"Autenticação concluída. URL: {self._page.url}")

    # --------------------------------------------------------- sessão / saúde

    async def sessao_ativa(self) -> bool:
        """Verifica se a sessão está ativa pela URL atual (sem navegar)."""
        url = self._page.url
        return (
            ".jsf" in url
            and "index.html" not in url
            and "login" not in url.split("/")[-1].lower()
            and url not in ("about:blank", "")
        )

    async def garantir_login(self):
        """Garante que o browser está autenticado via certificado.

        Verifica a URL atual; se não houver sessão ativa, faz login.
        """
        if await self.sessao_ativa():
            return
        logger.info("Sem sessão ativa. Fazendo login por certificado digital...")
        await self.login()

    # --------------------------------------------------------- seleção cliente

    async def selecionar_cliente(self, cnpj: str) -> str:
        """Seleciona o cliente no portal via tela 'Seleciona Cadastro'.

        Fluxo:
          1. Navega para selecionaCadastro.jsf
          2. Preenche o campo CPF/CNPJ com o CNPJ limpo
          3. Clica em Pesquisar
          4. Aguarda a tabela de resultados carregar
          5. Localiza a linha com o CNPJ e clica nela
          6. Aguarda o sidebar atualizar o bloco REPRESENTANDO
          7. Lê e retorna o nome do cliente do sidebar

        Raises:
            ClienteNaoEncontradoError: se o CNPJ não aparecer na tabela.
        """
        cnpj_limpo = _limpar_cnpj(cnpj)
        logger.info(f"Selecionando cliente CNPJ {cnpj}...")

        # Navega para a tela Seleciona Cadastro
        await self._page.goto(
            "https://novanfse.campinas.sp.gov.br/notafiscal/paginas/selecionacadastro/selecionaCadastro.jsf",
            wait_until="domcontentloaded",
            timeout=20_000,
        )
        try:
            await self._page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass
        await asyncio.sleep(1)

        # Preenche o campo CPF/CNPJ do filtro
        try:
            campo_cnpj = self._page.locator(
                "input[id*='cpfCnpj' i], input[name*='cpfCnpj' i]"
            ).first
            await campo_cnpj.wait_for(state="visible", timeout=10_000)
            await campo_cnpj.fill(cnpj_limpo)
        except Exception:
            preenchido = await self._page.evaluate(
                """(cnpj) => {
                    const inputs = [...document.querySelectorAll('input[type="text"], input:not([type])')];
                    const alvo = inputs.find(el => {
                        const ph = (el.placeholder || '').toLowerCase();
                        const id = (el.id || '').toLowerCase();
                        const nm = (el.name || '').toLowerCase();
                        const aria = (el.getAttribute('aria-label') || '').toLowerCase();
                        return ph.includes('cnpj') || ph.includes('cpf') ||
                               id.includes('cnpj') || id.includes('cpf') ||
                               nm.includes('cnpj') || nm.includes('cpf') ||
                               aria.includes('cnpj') || aria.includes('cpf');
                    }) || inputs[0];
                    if (!alvo) return false;
                    alvo.focus();
                    alvo.value = cnpj;
                    alvo.dispatchEvent(new Event('input',  {bubbles: true}));
                    alvo.dispatchEvent(new Event('change', {bubbles: true}));
                    return true;
                }""",
                cnpj_limpo,
            )
            if not preenchido:
                raise RuntimeError("Campo CPF/CNPJ do filtro não encontrado.")
        await asyncio.sleep(0.3)

        # Clica em Pesquisar via JS (mais confiável em JSF com overlays)
        clicou = await self._page.evaluate(
            """() => {
                const btns = [...document.querySelectorAll(
                    'button, a, input[type="submit"], input[type="button"]'
                )];
                const pesquisar = btns.find(b => {
                    const txt = (b.textContent || b.value || '').trim().toLowerCase();
                    return txt.includes('pesquisar');
                });
                if (pesquisar) {
                    pesquisar.click();
                    return true;
                }
                return false;
            }"""
        )
        if not clicou:
            try:
                await self._page.locator("text=Pesquisar").first.click(force=True, timeout=5_000)
            except Exception as e:
                raise RuntimeError(f"Botão Pesquisar não encontrado: {e}")
        logger.info("Pesquisa executada.")

        # Aguarda a tabela de resultados carregar
        try:
            await self._page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass
        await asyncio.sleep(1.5)

        # Localiza e clica na linha com o CNPJ
        encontrado = await self._page.evaluate(
            """(cnpjLimpo) => {
                const normalizar = t => (t || '').replace(/[.\\-\\/\\s]/g, '').trim();
                const rows = [...document.querySelectorAll('table tr, tbody tr')];
                for (const row of rows) {
                    const cells = [...row.querySelectorAll('td')];
                    if (!cells.length) continue;
                    for (const cell of cells) {
                        if (normalizar(cell.textContent) === cnpjLimpo) {
                            const link = row.querySelector('a, button, [role="button"]');
                            if (link) {
                                link.click();
                            } else {
                                row.click();
                            }
                            const ultimaCelula = cells[cells.length - 1];
                            return {
                                encontrado: true,
                                nome: (ultimaCelula?.textContent || '').trim(),
                            };
                        }
                    }
                }
                return { encontrado: false, nome: '' };
            }""",
            cnpj_limpo,
        )

        if not encontrado.get("encontrado"):
            diag = await self._page.evaluate(
                """() => {
                    const rows = [...document.querySelectorAll('table tr, tbody tr')];
                    const linhas = rows.slice(0, 10).map(r =>
                        [...r.querySelectorAll('td, th')]
                            .map(c => (c.textContent || '').trim())
                            .filter(Boolean).join(' | ')
                    ).filter(Boolean);
                    return {
                        total_linhas: rows.length,
                        primeiras_linhas: linhas,
                        body_preview: (document.body?.innerText || '').slice(0, 800),
                    };
                }"""
            )
            logger.warning(
                f"CNPJ {cnpj_limpo} não localizado. "
                f"Linhas na tabela: {diag.get('total_linhas')}. "
                f"Conteúdo: {diag.get('primeiras_linhas')}"
            )
            raise ClienteNaoEncontradoError(
                f"CNPJ {cnpj} não encontrado na tela Seleciona Cadastro. "
                "Verifique se a Procuração Eletrônica está configurada no portal."
            )

        nome_tabela = encontrado.get("nome", "")
        logger.info(f"Linha do cliente clicada. Nome na tabela: {nome_tabela}")

        # Aguarda o sidebar atualizar o bloco REPRESENTANDO
        try:
            await self._page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass
        await asyncio.sleep(2)

        # Lê o nome do cliente do sidebar (bloco REPRESENTANDO)
        nome = await self._ler_representando(cnpj_limpo)
        if not nome or nome == cnpj_limpo:
            nome = nome_tabela or cnpj_limpo
        logger.info(f"Cliente selecionado: {nome} (CNPJ: {cnpj})")
        return nome

    async def _ler_representando(self, cnpj_limpo: str) -> str:
        """Lê o bloco REPRESENTANDO no sidebar e retorna o nome do cliente."""
        try:
            bloco = await self._page.evaluate(
                """() => {
                    const todos = [...document.querySelectorAll('*')];
                    const el = todos.find(e =>
                        e.children.length === 0 &&
                        (e.textContent || '').trim() === 'REPRESENTANDO'
                    );
                    if (!el) return '';
                    let container = el.parentElement;
                    for (let i = 0; i < 3 && container; i++) {
                        const txt = (container.textContent || '').trim();
                        if (txt.length > 30) return txt;
                        container = container.parentElement;
                    }
                    return el.parentElement?.textContent?.trim() || '';
                }"""
            )

            if not bloco:
                bloco = await self._page.evaluate(
                    "() => document.body ? document.body.innerText : ''"
                )

            bloco_sem_pontuacao = re.sub(r"[.\-/]", "", bloco)
            if cnpj_limpo not in bloco_sem_pontuacao:
                logger.warning(
                    f"CNPJ {cnpj_limpo} não encontrado no bloco REPRESENTANDO. "
                    f"Bloco (primeiros 300 chars): {bloco[:300]!r}"
                )

            linhas = [l.strip() for l in bloco.splitlines() if l.strip()]
            for i, linha in enumerate(linhas):
                sem_pont = re.sub(r"[.\-/]", "", linha)
                if cnpj_limpo in sem_pont and i + 1 < len(linhas):
                    return linhas[i + 1]

            for linha in linhas:
                if "Nome" in linha and "-" in linha:
                    parte = linha.split("-", 1)[-1].strip()
                    if parte:
                        return parte

            return cnpj_limpo

        except Exception as e:
            logger.warning(f"Erro ao ler bloco REPRESENTANDO: {e}")
            return cnpj_limpo

    # ------------------------------------------------------- exportar relação

    async def exportar_relacao_notas(
        self,
        competencia_inicio: str,
        competencia_fim: str,
        pasta_destino: str,
        nome_cliente: str = "",
    ) -> str:
        logger.info(f"Exportando notas {competencia_inicio} a {competencia_fim}...")

        # Navega para a página de exportação com retry
        MAX_NAV = 3
        for tentativa_nav in range(1, MAX_NAV + 1):
            try:
                await self._page.goto(
                    "https://novanfse.campinas.sp.gov.br/notafiscal/paginas/exportacaonota/exportacaoNota.jsf",
                    wait_until="domcontentloaded",
                    timeout=20_000,
                )
                break
            except Exception as e:
                if tentativa_nav < MAX_NAV:
                    logger.warning(
                        f"goto exportacaoNota.jsf falhou ({e}). Aguardando e tentando novamente..."
                    )
                    await asyncio.sleep(3)
                else:
                    raise

        # Aguarda os campos de competência aparecerem
        await self._page.wait_for_selector(
            "input[type='text']", state="visible", timeout=15_000
        )
        await asyncio.sleep(1)

        await self._preencher_competencia(competencia_inicio, competencia_fim)
        await asyncio.sleep(0.5)

        await self._preencher_emissao(competencia_inicio, competencia_fim)
        await asyncio.sleep(0.5)

        await self._marcar_por_label("Ativa", marcar=True)
        await asyncio.sleep(0.3)

        await self._marcar_por_label("Prestado", marcar=True)
        await asyncio.sleep(0.3)

        Path(pasta_destino).mkdir(parents=True, exist_ok=True)

        # Tenta até 2 vezes acionar o download
        MAX_TENTATIVAS = 2
        TIMEOUT_MODAL_S = 10

        ultimo_erro = None
        download = None
        for tentativa in range(1, MAX_TENTATIVAS + 1):
            try:
                async with self._page.expect_download(timeout=30_000) as dl_info:
                    await self._clicar_botao_gerar()
                    logger.info(
                        f"Tentativa {tentativa}/{MAX_TENTATIVAS}: "
                        f"Botão 'Gerar Relação Notas' acionado."
                    )

                    # Aguarda modal de confirmação aparecer
                    try:
                        await self._page.wait_for_selector(
                            "text=Deseja Realmente Confirmar",
                            timeout=TIMEOUT_MODAL_S * 1000,
                        )
                        logger.info("Modal de confirmação detectado — clicando Download...")
                        await self._clicar_modal_download()
                        logger.info("Aguardando download...")
                    except Exception:
                        # Modal não apareceu — interrompe essa tentativa
                        logger.warning(
                            f"Modal não apareceu em {TIMEOUT_MODAL_S}s "
                            f"(tentativa {tentativa}/{MAX_TENTATIVAS})."
                        )
                        raise RuntimeError("Modal de confirmação não apareceu.")

                # Se chegou aqui, o download começou — sai do loop
                download = await dl_info.value
                break

            except Exception as e:
                ultimo_erro = e
                if tentativa < MAX_TENTATIVAS:
                    logger.warning(
                        f"Tentativa {tentativa} falhou: {e}. Tentando novamente..."
                    )
                    await asyncio.sleep(1)
                else:
                    logger.error(
                        f"Modal não apareceu após {MAX_TENTATIVAS} tentativas. "
                        f"Pulando cliente."
                    )
                    raise RuntimeError(
                        f"Modal de confirmação não apareceu após {MAX_TENTATIVAS} tentativas. "
                        f"Cliente pulado."
                    ) from ultimo_erro

        # Sucesso — salva o arquivo
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        seguro = _nome_seguro(nome_cliente)
        extensao = Path(download.suggested_filename).suffix or ".xml"
        filename = f"{seguro}_{competencia_inicio.replace('/', '-')}_{ts}{extensao}"
        destino = os.path.join(pasta_destino, filename)
        await download.save_as(destino)
        logger.info(f"Arquivo salvo: {destino}")
        return destino

    async def _clicar_modal_download(self):
        clicou = await self._page.evaluate("""
            () => {
                const btns = [...document.querySelectorAll('a, button')];
                const btn = btns.find(b => b.textContent.trim().includes('Download'));
                if (!btn) return false;
                btn.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));
                return true;
            }
        """)
        if not clicou:
            await self._page.locator("text=Download").first.click(force=True)
        logger.info("Download confirmado.")

    async def _clicar_botao_gerar(self):
        """Clica no botão 'Gerar Relação Notas' usando JS para garantir que o
        elemento clicável correto (o <a> ou <button> pai) seja atingido."""
        clicou = await self._page.evaluate("""
            () => {
                const candidatos = [
                    ...document.querySelectorAll('a, button, input[type="submit"]')
                ];
                const btn = candidatos.find(el =>
                    el.textContent && el.textContent.includes('Gerar Rela')
                );
                if (!btn) return false;
                btn.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));
                return true;
            }
        """)
        if clicou:
            logger.info("Botão 'Gerar Relação Notas' clicado via JS.")
        else:
            logger.warning("Botão não encontrado via JS — tentando Playwright force click.")
            await self._page.locator("text=Gerar Relação Notas").first.click(force=True)

    async def _marcar_por_label(self, texto: str, marcar: bool = True):
        """Marca/desmarca um checkbox ou radio cujo label contém o texto dado."""
        try:
            await self._page.evaluate(
                """({texto, marcar}) => {
                    const labels = Array.from(document.querySelectorAll('label'));
                    const label = labels.find(l => l.textContent.trim().includes(texto));
                    if (!label) return false;
                    let input = label.querySelector('input[type="checkbox"], input[type="radio"]');
                    if (!input && label.htmlFor) {
                        input = document.getElementById(label.htmlFor);
                    }
                    if (!input) {
                        input = label.previousElementSibling;
                        if (!input || (input.type !== 'checkbox' && input.type !== 'radio')) {
                            input = label.parentElement.querySelector('input[type="checkbox"], input[type="radio"]');
                        }
                    }
                    if (!input) return false;
                    if (input.checked !== marcar) {
                        input.click();
                    }
                    return true;
                }""",
                {"texto": texto, "marcar": marcar},
            )
            logger.info(f"Campo '{texto}' marcado={marcar}.")
        except Exception as e:
            logger.warning(f"Não consegui marcar '{texto}': {e}")

    async def _preencher_emissao(self, competencia_inicio: str, competencia_fim: str):
        """Preenche os campos de Data de Emissão com o primeiro dia do mês inicial
        e o último dia do mês final da competência.
        Formato de entrada: MM/AAAA → saída no campo: DD/MM/AAAA
        """
        mes_ini, ano_ini = int(competencia_inicio[:2]), int(competencia_inicio[3:])
        mes_fim, ano_fim = int(competencia_fim[:2]), int(competencia_fim[3:])

        ultimo_dia = calendar.monthrange(ano_fim, mes_fim)[1]
        data_inicio = f"01/{mes_ini:02d}/{ano_ini}"
        data_fim    = f"{ultimo_dia:02d}/{mes_fim:02d}/{ano_fim}"

        await self._page.evaluate(
            """({inicio, fim}) => {
                const inputs = Array.from(document.querySelectorAll('input[type="text"]'));
                if (inputs[2]) {
                    inputs[2].value = inicio;
                    inputs[2].dispatchEvent(new Event('input',  {bubbles:true}));
                    inputs[2].dispatchEvent(new Event('change', {bubbles:true}));
                }
                if (inputs[3]) {
                    inputs[3].value = fim;
                    inputs[3].dispatchEvent(new Event('input',  {bubbles:true}));
                    inputs[3].dispatchEvent(new Event('change', {bubbles:true}));
                }
            }""",
            {"inicio": data_inicio, "fim": data_fim},
        )
        logger.info(f"Emissão preenchida: {data_inicio} a {data_fim}")

    async def _preencher_competencia(self, inicio: str, fim: str):
        date_inputs = self._page.locator("input[type='text']")

        campo_inicio = date_inputs.nth(0)
        await campo_inicio.click(click_count=3)
        await campo_inicio.fill(inicio)

        campo_fim = date_inputs.nth(1)
        await campo_fim.click(click_count=3)
        await campo_fim.fill(fim)

        await campo_fim.press("Tab")
        await asyncio.sleep(0.3)


# ------------------------------------------------------------------ helpers

def _limpar_cnpj(texto: str) -> str:
    return "".join(c for c in texto if c.isdigit())


def _nome_seguro(nome: str) -> str:
    return "".join(c if c.isalnum() or c in " _-" else "_" for c in nome).strip()[:50]
