import asyncio
import calendar
import logging
import os
import re
from pathlib import Path
from datetime import datetime

import httpx
from playwright.async_api import async_playwright, Page, BrowserContext

from config import Config

logger = logging.getLogger(__name__)

# playwright-stealth: API mudou entre versões; tenta as principais
try:
    from playwright_stealth import stealth_async  # v1.x
    _STEALTH_API = "v1"
except ImportError:
    try:
        from playwright_stealth import Stealth  # v2.x
        _STEALTH_API = "v2"
    except ImportError:
        _STEALTH_API = None


async def _aplicar_stealth(page: "Page"):
    """Aplica fingerprint anti-bot na página (compatível com v1 e v2)."""
    if _STEALTH_API == "v1":
        await stealth_async(page)
    elif _STEALTH_API == "v2":
        await Stealth().apply_stealth_async(page)
    else:
        logger.warning(
            "playwright-stealth não instalado. Rode: pip install playwright-stealth"
        )

PORTAL_URL = "https://novanfse.campinas.sp.gov.br/notafiscal/paginas/portal/index.html#/"


class ClienteNaoEncontradoError(Exception):
    pass


_INIT_SCRIPT = (
    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
)

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
        self._headless: bool = True

    # ------------------------------------------------------------------ setup

    async def iniciar(self, headless: bool = True):
        """Inicia o browser tentando 3 abordagens em sequência automática.

        Abordagem 1 — Chromium embutido headless com flags SSL/segurança
        Abordagem 2 — Chrome instalado no sistema em modo headless
        Abordagem 3 — Chrome instalado no sistema em modo visível (fallback)

        Se headless=False (--visible), vai direto para a abordagem visível.
        Cada abordagem testa a navegação real ao portal antes de ser aceita.
        """
        self._headless = headless
        self._playwright = await async_playwright().start()

        # Monta a lista de tentativas de acordo com o modo solicitado
        if not headless:
            # Modo --visible: abre Chrome visível diretamente (sem tentar headless)
            tentativas = [
                dict(
                    headless=False,
                    channel="chrome",
                    args=["--no-sandbox", "--ignore-certificate-errors"],
                ),
                # fallback: Chromium embutido visível (se Chrome não instalado)
                dict(
                    headless=False,
                    args=["--no-sandbox", "--ignore-certificate-errors"],
                ),
            ]
        else:
            tentativas = [
                # Abordagem 1: Chromium embutido headless com flags SSL
                dict(
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--ignore-certificate-errors",
                        "--ignore-ssl-errors",
                        "--disable-web-security",
                        "--allow-running-insecure-content",
                        "--disable-blink-features=AutomationControlled",
                    ],
                ),
                # Abordagem 2: Chrome instalado headless (fingerprint real)
                dict(
                    headless=True,
                    channel="chrome",
                    args=["--no-sandbox", "--ignore-certificate-errors"],
                ),
                # Abordagem 3: Chrome instalado visível (último recurso headless)
                dict(
                    headless=False,
                    channel="chrome",
                    args=["--no-sandbox"],
                ),
            ]

        ultimo_erro = None
        for i, opcoes in enumerate(tentativas, 1):
            canal = opcoes.get("channel", "chromium")
            modo  = "headless" if opcoes.get("headless") else "visível"
            logger.info(f"Tentando iniciar browser (abordagem {i}/{len(tentativas)}): {canal} {modo}...")
            try:
                self._browser = await self._playwright.chromium.launch(**opcoes)
                self._context = await self._browser.new_context(
                    accept_downloads=True,
                    ignore_https_errors=True,
                    viewport={"width": 1280, "height": 800},
                    user_agent=_USER_AGENT,
                )
                await self._context.add_init_script(_INIT_SCRIPT)
                self._page = await self._context.new_page()
                await _aplicar_stealth(self._page)
                self._page.set_default_timeout(self.config.TIMEOUT_MS)

                # Teste real: tenta navegar para o portal e verifica se carregou
                await self._page.goto(
                    PORTAL_URL, wait_until="domcontentloaded", timeout=30_000
                )
                if self._page.url.startswith("chrome-error://"):
                    raise RuntimeError(
                        f"Portal redirecionou para chrome-error:// "
                        f"(SSL/rede bloqueada para {canal} {modo})"
                    )

                stealth_status = "ativo" if _STEALTH_API else "indisponível"
                logger.info(
                    f"Browser iniciado com sucesso: {canal} ({modo}) | stealth {stealth_status}"
                )
                self._headless = opcoes.get("headless", True)
                return  # sucesso — para aqui

            except Exception as e:
                ultimo_erro = e
                logger.warning(f"Abordagem {i} falhou: {e}")
                await self._limpar_browser()

        raise RuntimeError(
            f"Nenhuma abordagem conseguiu abrir o portal NFSe após {len(tentativas)} tentativas.\n"
            f"Último erro: {ultimo_erro}\n\n"
            "Verifique:\n"
            "  1) Conexão com a internet\n"
            "  2) Se o portal abre no Chrome/Edge normalmente\n"
            "  3) Se o antivírus/firewall está bloqueando o Playwright\n"
            "  4) Execute: python diagnostico.py — para diagnóstico detalhado"
        )

    async def _limpar_browser(self):
        """Fecha context e browser sem lançar exceções (usado no retry)."""
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
        self._browser  = None
        self._context  = None
        self._page     = None

    async def fechar(self):
        await self._limpar_browser()
        if self._playwright:
            await self._playwright.stop()
        logger.info("Browser encerrado.")

    # ------------------------------------------------------------------ login

    async def login(self, cnpj: str, senha: str):
        """Faz login no portal.

        - Modo visível (--visible): pré-preenche o CNPJ e aguarda o usuário
          completar a senha e resolver o reCAPTCHA manualmente. A sessão fica
          salva em disco; execuções futuras em headless não precisam re-logar.

        - Modo headless: preenche tudo via automação e tenta submeter com JS
          dispatch (contorna bloqueio de pointer events do iframe reCAPTCHA).
          Funciona se o reCAPTCHA não acionar o desafio de imagens.
        """
        # Sempre navega para a home do portal — garante estado limpo do SPA.
        logger.info("Acessando portal NFSe Campinas...")
        await self._page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=30_000)

        # Aguarda o SPA carregar completamente (networkidle + folga)
        try:
            await self._page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass
        await asyncio.sleep(2)

        # Fecha qualquer overlay inicial (comunicados, cookies, etc.)
        await self._page.keyboard.press("Escape")
        await asyncio.sleep(0.5)

        # Clica em AUTENTICAR para abrir a tela de login.
        # Timeout generoso pois o SPA pode demorar para renderizar.
        autenticar = self._page.locator("text=AUTENTICAR").first
        await autenticar.wait_for(state="visible", timeout=30_000)
        await autenticar.dispatch_event("click")
        logger.info("Clicou em AUTENTICAR.")
        await asyncio.sleep(1)

        # Preenche o CNPJ (em ambos os modos)
        await self._page.locator("input[name='cpfCnpj']").fill(_limpar_cnpj(cnpj))

        if not self._headless:
            # ── Modo visível: usuário preenche senha e resolve reCAPTCHA ──
            logger.info(
                "Browser aberto. Preencha a senha e resolva o reCAPTCHA. "
                "Você tem até 5 minutos."
            )
            await self._page.wait_for_url("**/*.jsf**", timeout=300_000)
        else:
            # ── Modo headless: automação completa com stealth + CapSolver ──
            await self._page.locator("input[name='senha']").fill(senha)
            await asyncio.sleep(1)
            logger.info("CNPJ e senha preenchidos.")

            # Resolve o reCAPTCHA (stealth → fallback CapSolver se configurado)
            resolvido = await self._resolver_recaptcha()
            if not resolvido:
                raise RuntimeError(
                    "reCAPTCHA não resolvido em modo headless. Opções:\n"
                    "  1) Rode com --visible para login manual.\n"
                    "  2) Verifique CAPSOLVER_API_KEY no .env (deve ter saldo).\n"
                    "  3) Verifique se 'playwright-stealth' está instalado."
                )

            # Re-preenche CNPJ e senha via teclado real (Angular ngModel
            # escuta 'input' event que apenas eventos reais de teclado disparam).
            await self._injetar_credenciais_angular(_limpar_cnpj(cnpj), senha)

            logger.info("Credenciais preenchidas. Submetendo login...")
            await asyncio.sleep(1)

            # Submete o form (cascata de estratégias para contornar overlay reCAPTCHA)
            await self._submeter_login()

            try:
                await self._page.wait_for_url("**/*.jsf**", timeout=45_000)
            except Exception:
                url_atual = self._page.url
                texto = await self._page.evaluate(
                    "() => (document.body && document.body.innerText) || ''"
                )
                logger.error(
                    f"Login não navegou para .jsf após 45s. URL: {url_atual}\n"
                    f"Texto: {texto[:300]!r}"
                )
                raise RuntimeError(
                    "Submit do login não redirecionou para o portal autenticado. "
                    "O token reCAPTCHA pode ter sido rejeitado pelo servidor "
                    "ou as credenciais estão incorretas."
                )

        logger.info(f"Login realizado com sucesso. URL: {self._page.url}")

    async def _injetar_credenciais_angular(self, cnpj: str, senha: str):
        """Preenche CNPJ e senha via eventos reais de teclado.

        Angular reactive forms (FormControl) só sincroniza com o DOM quando
        recebe eventos de teclado nativos (keydown/input/keyup), não quando
        `input.value` é setado por JS. Programmatic setters não funcionam.

        Estratégia:
          1. Foco no input via JS (bypassa overlay do reCAPTCHA)
          2. Limpa via Ctrl+A + Delete (eventos de teclado reais)
          3. Digita char por char via page.keyboard.type()

        Isso simula exatamente um usuário digitando — Angular pega 100%.
        """
        try:
            # CNPJ
            await self._page.evaluate(
                """() => {
                    const el = document.querySelector('input[name="cpfCnpj"]');
                    if (el) el.focus();
                }"""
            )
            await asyncio.sleep(0.2)
            await self._page.keyboard.press("Control+A")
            await self._page.keyboard.press("Delete")
            await self._page.keyboard.type(cnpj, delay=15)

            # Senha
            await self._page.evaluate(
                """() => {
                    const el = document.querySelector('input[name="senha"]');
                    if (el) el.focus();
                }"""
            )
            await asyncio.sleep(0.2)
            await self._page.keyboard.press("Control+A")
            await self._page.keyboard.press("Delete")
            await self._page.keyboard.type(senha, delay=15)

            # Tab para tirar foco e disparar validators
            await self._page.keyboard.press("Tab")

            logger.info(
                "CNPJ e senha injetados via page.keyboard.type "
                "(eventos de teclado reais para Angular)."
            )
        except Exception as e:
            logger.warning(f"Falha ao injetar credenciais via teclado: {e}")

    async def _submeter_login(self):
        """Submete o form de login tentando estratégias em cascata.

        Após resolver o reCAPTCHA, o iframe do desafio (bframe) frequentemente
        permanece como overlay invisível bloqueando cliques DE COORDENADA.
        A solução robusta é usar JS para chamar .click() diretamente no
        elemento Angular — isso executa o click handler ignorando overlay.
        """
        # Remove overlays do reCAPTCHA por garantia (mas o JS click não depende disso)
        await self._page.evaluate(
            """() => {
                document.querySelectorAll(
                    'iframe[src*="bframe"], iframe[name^="c-"]'
                ).forEach(i => {
                    let el = i;
                    while (el && el !== document.body) {
                        const style = window.getComputedStyle(el);
                        if (parseInt(style.zIndex || '0') > 1000 ||
                            style.position === 'fixed' ||
                            style.position === 'absolute') {
                            el.style.display = 'none';
                            el.style.pointerEvents = 'none';
                            break;
                        }
                        el = el.parentElement;
                    }
                    i.style.display = 'none';
                    i.style.pointerEvents = 'none';
                });
                document.querySelectorAll('div').forEach(d => {
                    const s = window.getComputedStyle(d);
                    if (s.position === 'fixed' && parseInt(s.zIndex || '0') >= 2000000000) {
                        d.style.display = 'none';
                    }
                });
            }"""
        )
        await asyncio.sleep(0.3)

        # Estratégia 1 (preferida): JS chama .click() direto no elemento ENTRAR.
        # Isso ignora overlays — o click handler do Angular é executado mesmo se
        # outro elemento estaria sobreposto na coordenada.
        clicou = await self._page.evaluate(
            """() => {
                // Procura botão visível contendo "ENTRAR" (não AUTENTICAR)
                const candidatos = [...document.querySelectorAll(
                    'button, a, [role="button"]'
                )];
                const entrar = candidatos.find(el => {
                    const txt = (el.textContent || '').trim().toUpperCase();
                    return txt === 'ENTRAR' || txt.endsWith('ENTRAR') &&
                        !txt.includes('AUTENTICAR') &&
                        el.offsetParent !== null;
                });
                if (entrar) {
                    entrar.click();
                    return {clicked: true, tag: entrar.tagName, text: entrar.textContent.trim().slice(0, 30)};
                }
                // Fallback: button[type=submit]
                const submit = document.querySelector('button[type="submit"]');
                if (submit) {
                    submit.click();
                    return {clicked: true, tag: submit.tagName, text: 'submit-button'};
                }
                return {clicked: false};
            }"""
        )
        if clicou.get("clicked"):
            logger.info(f"Submit: JS .click() em <{clicou.get('tag')}> '{clicou.get('text')}'.")
            return

        # Estratégia 2: clique forçado no texto ENTRAR (Playwright)
        try:
            await self._page.locator("text=ENTRAR").first.click(force=True, timeout=3_000)
            logger.info("Submit: Playwright force click em 'text=ENTRAR'.")
            return
        except Exception:
            pass

        # Estratégia 3: pressiona Enter no campo de senha (último recurso)
        try:
            await self._page.locator("input[name='senha']").press("Enter", timeout=2_000)
            logger.info("Submit: Enter no campo senha (fallback).")
            return
        except Exception as e:
            raise RuntimeError(f"Não foi possível submeter o login: {e}")

    # ---------------------------------------------------------- reCAPTCHA

    async def _resolver_recaptcha(self) -> bool:
        """Orquestra a resolução do reCAPTCHA.

        Estratégia em cascata:
          1. Stealth (custo zero) — tenta resolver só com fingerprint anti-bot
          2. CapSolver API (pago) — fallback se stealth falhar
        """
        # Tenta stealth primeiro (custo zero)
        resolvido = await self._tentar_recaptcha_stealth()
        if resolvido:
            return True

        # Fallback: CapSolver
        if self.config.CAPSOLVER_API_KEY:
            logger.info("Stealth não resolveu. Tentando CapSolver...")
            return await self._resolver_recaptcha_capsolver()

        logger.error(
            "reCAPTCHA não resolvido. Configure CAPSOLVER_API_KEY no .env "
            "ou rode com --visible para login manual."
        )
        return False

    async def _tentar_recaptcha_stealth(self) -> bool:
        """Tenta resolver o reCAPTCHA v2 checkbox apenas com stealth.

        Clica no checkbox "Não sou um robô" e aguarda até 10s para ver se
        o Google aceita automaticamente (sem desafio visual). Retorna True
        se o token foi gerado, False caso contrário.
        """
        try:
            recaptcha_iframe = self._page.frame_locator(
                "iframe[title*='reCAPTCHA'], iframe[src*='recaptcha'][src*='anchor']"
            ).first

            checkbox = recaptcha_iframe.locator("#recaptcha-anchor")
            await checkbox.wait_for(state="visible", timeout=10_000)
            await checkbox.click()
            logger.info("Checkbox reCAPTCHA clicado.")

            for tentativa in range(10):
                await asyncio.sleep(1)
                resolvido = await self._page.evaluate(
                    """() => {
                        // 1) Verifica se o checkbox foi marcado (aria-checked)
                        const frames = document.querySelectorAll('iframe[src*="recaptcha"]');
                        for (const frame of frames) {
                            try {
                                const anchor = frame.contentDocument
                                    ?.querySelector('#recaptcha-anchor');
                                if (anchor?.getAttribute('aria-checked') === 'true') {
                                    return true;
                                }
                            } catch (e) {}
                        }
                        // 2) Verifica se o token g-recaptcha-response foi preenchido
                        const response = document.querySelector(
                            'textarea[name="g-recaptcha-response"], input[name="g-recaptcha-response"]'
                        );
                        return !!(response && response.value && response.value.length > 0);
                    }"""
                )
                if resolvido:
                    logger.info(
                        f"reCAPTCHA resolvido automaticamente via stealth ({tentativa + 1}s)."
                    )
                    return True

            logger.warning("reCAPTCHA não resolvido automaticamente após 10s.")
            return False

        except Exception as e:
            logger.warning(f"Erro ao interagir com reCAPTCHA via stealth: {e}")
            return False

    async def _resolver_recaptcha_capsolver(self) -> bool:
        """
        Resolve o reCAPTCHA v2 via CapSolver API.
        Documentação: https://docs.capsolver.com/en/guide/captcha/recaptchaV2/
        """
        api_key = self.config.CAPSOLVER_API_KEY
        if not api_key:
            logger.warning("CAPSOLVER_API_KEY não configurada no .env.")
            return False

        try:
            # Extrai a sitekey do reCAPTCHA da página (atributo direto OU iframe ?k=)
            sitekey = await self._page.evaluate(
                """
                () => {
                    // Tenta atributo data-sitekey direto
                    const el = document.querySelector('[data-sitekey]');
                    if (el) return el.getAttribute('data-sitekey');

                    // Tenta dentro do iframe do reCAPTCHA
                    const iframes = document.querySelectorAll(
                        'iframe[src*="recaptcha"]'
                    );
                    for (const iframe of iframes) {
                        const src = iframe.getAttribute('src') || '';
                        const match = src.match(/[?&]k=([^&]+)/);
                        if (match) return match[1];
                    }
                    return null;
                }
                """
            )

            if not sitekey:
                logger.error("Sitekey do reCAPTCHA não encontrada na página.")
                return False

            logger.info(
                f"Resolvendo reCAPTCHA via CapSolver (sitekey: {sitekey[:20]}...)."
            )
            url_atual = self._page.url

            async with httpx.AsyncClient(timeout=30) as client:
                # Cria a tarefa no CapSolver
                resp = await client.post(
                    "https://api.capsolver.com/createTask",
                    json={
                        "clientKey": api_key,
                        "task": {
                            "type": "ReCaptchaV2TaskProxyLess",
                            "websiteURL": url_atual,
                            "websiteKey": sitekey,
                        },
                    },
                )
                data = resp.json()

                if data.get("errorId", 0) != 0:
                    logger.error(
                        f"CapSolver erro ao criar tarefa: {data.get('errorDescription')}"
                    )
                    return False

                task_id = data.get("taskId")
                if not task_id:
                    logger.error(f"CapSolver não retornou taskId: {data}")
                    return False

                logger.info(
                    f"Tarefa CapSolver criada: {task_id}. Aguardando resolução..."
                )

                # Aguarda resolução (até 120 segundos)
                for tentativa in range(60):
                    await asyncio.sleep(2)
                    resp = await client.post(
                        "https://api.capsolver.com/getTaskResult",
                        json={"clientKey": api_key, "taskId": task_id},
                    )
                    result = resp.json()

                    if result.get("errorId", 0) != 0:
                        logger.error(
                            f"CapSolver erro: {result.get('errorDescription')}"
                        )
                        return False

                    if result.get("status") == "ready":
                        token = result["solution"]["gRecaptchaResponse"]
                        logger.info(
                            f"Token obtido ({len(token)} chars). Injetando na página..."
                        )

                        # Injeta o token na página + dispara callback do reCAPTCHA
                        await self._page.evaluate(
                            """
                            (token) => {
                                // 1) Injeta no campo padrão g-recaptcha-response
                                //    Dispatch 'input' (ngModel) + 'change' para Angular reagir.
                                const fields = document.querySelectorAll(
                                    '[name="g-recaptcha-response"]'
                                );
                                fields.forEach(f => {
                                    f.value = token;
                                    f.style.display = 'block';
                                    f.dispatchEvent(new Event('input',  {bubbles: true}));
                                    f.dispatchEvent(new Event('change', {bubbles: true}));
                                    f.dispatchEvent(new Event('blur',   {bubbles: true}));
                                });

                                // 1.5) Marca o checkbox como "checked" no anchor iframe
                                document.querySelectorAll(
                                    'iframe[src*="anchor"]'
                                ).forEach(iframe => {
                                    try {
                                        const anchor = iframe.contentDocument
                                            ?.querySelector('#recaptcha-anchor');
                                        if (anchor) {
                                            anchor.setAttribute('aria-checked', 'true');
                                            anchor.classList.add('recaptcha-checkbox-checked');
                                        }
                                    } catch(e) {}
                                });

                                // 1.6) Procura componente Angular ng-recaptcha e chama seu resolved.emit()
                                try {
                                    if (window.ng && window.ng.getComponent) {
                                        document.querySelectorAll(
                                            're-captcha, ng-recaptcha, [ng-recaptcha]'
                                        ).forEach(el => {
                                            const comp = window.ng.getComponent(el);
                                            if (comp) {
                                                if (comp.resolved && typeof comp.resolved.emit === 'function') {
                                                    comp.resolved.emit(token);
                                                }
                                                if (typeof comp.onResponse === 'function') {
                                                    comp.onResponse(token);
                                                }
                                                if (typeof comp.onSuccess === 'function') {
                                                    comp.onSuccess(token);
                                                }
                                            }
                                        });
                                    }
                                } catch(e) {}

                                // 2) Dispara callback definido em data-callback
                                const el = document.querySelector('[data-callback]');
                                if (el) {
                                    const cb = el.getAttribute('data-callback');
                                    if (cb && typeof window[cb] === 'function') {
                                        try { window[cb](token); } catch(e) {}
                                    }
                                }

                                // 3) Busca recursiva por callbacks em ___grecaptcha_cfg.clients
                                //    (o reCAPTCHA armazena callbacks em estrutura aninhada)
                                try {
                                    const cfg = window.___grecaptcha_cfg ||
                                                (window.grecaptcha && window.grecaptcha.cfg);
                                    if (cfg && cfg.clients) {
                                        const visitados = new WeakSet();
                                        const buscarCallback = (obj, profundidade) => {
                                            if (!obj || profundidade > 6) return;
                                            if (typeof obj === 'object' && !visitados.has(obj)) {
                                                visitados.add(obj);
                                                for (const k in obj) {
                                                    try {
                                                        const v = obj[k];
                                                        if (typeof v === 'function' &&
                                                            (k === 'callback' || k === 'fulfilled' ||
                                                             v.name === 'callback')) {
                                                            try { v(token); } catch(e) {}
                                                        } else if (typeof v === 'object') {
                                                            buscarCallback(v, profundidade + 1);
                                                        }
                                                    } catch(e) {}
                                                }
                                            }
                                        };
                                        Object.keys(cfg.clients).forEach(id => {
                                            buscarCallback(cfg.clients[id], 0);
                                        });
                                    }
                                } catch(e) {}
                            }
                            """,
                            token,
                        )

                        await asyncio.sleep(1)
                        logger.info("reCAPTCHA resolvido via CapSolver.")
                        return True

                    if tentativa % 5 == 0:
                        logger.info(f"Aguardando CapSolver... ({tentativa * 2}s)")

            logger.warning("CapSolver não resolveu dentro do tempo limite (120s).")
            return False

        except httpx.RequestError as e:
            logger.error(f"Erro de rede ao contatar CapSolver: {e}")
            return False
        except Exception as e:
            logger.error(f"Erro inesperado no CapSolver: {e}", exc_info=True)
            return False

    # --------------------------------------------------------- seleção cliente

    async def selecionar_cliente(self, cnpj: str) -> str:
        """
        Seleciona o cliente no portal via tela 'Seleciona Cadastro'.

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

        # Caso especial: se o CNPJ é o próprio CNPJ logado (MAFRA), não precisa
        # selecionar via Seleciona Cadastro — o login já autenticou esse CNPJ.
        # Detecta pelo CNPJ_PORTAL configurado no .env.
        cnpj_logado = _limpar_cnpj(self.config.CNPJ_PORTAL or "")
        if cnpj_limpo == cnpj_logado:
            logger.info(
                f"CNPJ {cnpj} é o próprio CNPJ logado — não precisa "
                "selecionar via Seleciona Cadastro."
            )
            nome = await self._ler_nome_logado()
            logger.info(f"Cliente selecionado: {nome} (CNPJ: {cnpj})")
            return nome

        # 1. Navega para a tela Seleciona Cadastro
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

        # 2. Preenche o campo CPF/CNPJ do filtro
        # O campo pode ter id ou name contendo "cpfCnpj" ou ser o primeiro input visível.
        try:
            campo_cnpj = self._page.locator(
                "input[id*='cpfCnpj' i], input[name*='cpfCnpj' i]"
            ).first
            await campo_cnpj.wait_for(state="visible", timeout=10_000)
            await campo_cnpj.fill(cnpj_limpo)
        except Exception:
            # Fallback: usa o primeiro input do formulário de filtro
            preenchido = await self._page.evaluate(
                """(cnpj) => {
                    // Procura input cujo label/placeholder/aria menciona CPF ou CNPJ
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

        # 3. Clica em Pesquisar via JS (mais confiável em JSF com overlays)
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

        # 4. Aguarda a tabela de resultados carregar
        try:
            await self._page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass
        await asyncio.sleep(1.5)

        # 5. Localiza e clica na linha com o CNPJ
        # A tabela tem colunas: checkmark | CPF/CNPJ | Nome/Nome Empresarial.
        # O portal exibe o CNPJ formatado (XX.XXX.XXX/XXXX-XX), então comparamos
        # após remover pontuação.
        encontrado = await self._page.evaluate(
            """(cnpjLimpo) => {
                const normalizar = t => (t || '').replace(/[.\\-\\/\\s]/g, '').trim();

                const rows = [...document.querySelectorAll('table tr, tbody tr')];
                for (const row of rows) {
                    const cells = [...row.querySelectorAll('td')];
                    if (!cells.length) continue;
                    for (const cell of cells) {
                        if (normalizar(cell.textContent) === cnpjLimpo) {
                            // Tenta clicar em link/checkmark dentro da linha;
                            // se não houver, clica na linha inteira.
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
            # Diagnóstico: captura conteúdo da tabela para entender o que carregou
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

        # 6. Aguarda o sidebar atualizar o bloco REPRESENTANDO
        try:
            await self._page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass
        await asyncio.sleep(2)

        # 7. Lê o nome do cliente do sidebar (bloco REPRESENTANDO)
        nome = await self._ler_representando(cnpj_limpo)
        # Se o sidebar não trouxe um nome legível, usa o que veio da tabela
        if not nome or nome == cnpj_limpo:
            nome = nome_tabela or cnpj_limpo
        logger.info(f"Cliente selecionado: {nome} (CNPJ: {cnpj})")
        return nome

    async def _ler_nome_logado(self) -> str:
        """Lê o nome da empresa logada (sidebar bloco USUÁRIO).

        Usado quando o CNPJ alvo é o próprio CNPJ que fez login — não precisa
        passar pela tela Seleciona Cadastro.
        """
        try:
            # Garante que estamos em uma .jsf (sidebar carregado)
            if "index.html" in self._page.url or "selecionaCadastro" in self._page.url:
                await self._page.goto(
                    "https://novanfse.campinas.sp.gov.br/notafiscal/paginas/login/bemVindo.jsf",
                    wait_until="domcontentloaded",
                    timeout=20_000,
                )
                try:
                    await self._page.wait_for_load_state("networkidle", timeout=10_000)
                except Exception:
                    pass
                await asyncio.sleep(1)

            bloco = await self._page.evaluate(
                "() => document.body ? document.body.innerText : ''"
            )
            for linha in bloco.splitlines():
                linha = linha.strip()
                if "Nome" in linha and "-" in linha:
                    nome = linha.split("-", 1)[-1].strip()
                    if nome:
                        return nome
            return _limpar_cnpj(self.config.CNPJ_PORTAL or "")
        except Exception as e:
            logger.warning(f"Erro ao ler nome do CNPJ logado: {e}")
            return ""

    async def _ler_representando(self, cnpj_limpo: str) -> str:
        """
        Lê o bloco REPRESENTANDO no sidebar e retorna o nome do cliente.
        Valida que o CNPJ exibido corresponde ao CNPJ esperado.
        """
        try:
            # Lê o bloco REPRESENTANDO completo
            bloco = await self._page.evaluate(
                """() => {
                    // Procura o elemento de texto puro que contém "REPRESENTANDO"
                    const todos = [...document.querySelectorAll('*')];
                    const el = todos.find(e =>
                        e.children.length === 0 &&
                        (e.textContent || '').trim() === 'REPRESENTANDO'
                    );
                    if (!el) return '';
                    // Sobe até um contêiner que provavelmente contém CNPJ + nome.
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
                # Fallback: texto completo da página
                bloco = await self._page.evaluate(
                    "() => document.body ? document.body.innerText : ''"
                )

            # Valida que o CNPJ correto está no bloco
            bloco_sem_pontuacao = re.sub(r"[.\-/]", "", bloco)
            if cnpj_limpo not in bloco_sem_pontuacao:
                logger.warning(
                    f"CNPJ {cnpj_limpo} não encontrado no bloco REPRESENTANDO. "
                    f"Bloco (primeiros 300 chars): {bloco[:300]!r}"
                )

            # Extrai o nome: linha imediatamente após o CNPJ no bloco
            linhas = [l.strip() for l in bloco.splitlines() if l.strip()]
            for i, linha in enumerate(linhas):
                sem_pont = re.sub(r"[.\-/]", "", linha)
                if cnpj_limpo in sem_pont and i + 1 < len(linhas):
                    return linhas[i + 1]

            # Fallback: se acharmos uma linha com "Nome - X", extrai depois do hífen
            for linha in linhas:
                if "Nome" in linha and "-" in linha:
                    parte = linha.split("-", 1)[-1].strip()
                    if parte:
                        return parte

            return cnpj_limpo  # último recurso

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

        # Navega diretamente para a página de exportação (mais confiável que clicar no menu)
        await self._page.goto(
            "https://novanfse.campinas.sp.gov.br/notafiscal/paginas/exportacaonota/exportacaoNota.jsf",
            wait_until="domcontentloaded",
            timeout=20_000,
        )
        # Aguarda os campos de competência aparecerem
        await self._page.wait_for_selector("input[type='text']", state="visible", timeout=15_000)
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

        async with self._page.expect_download(timeout=90_000) as dl_info:
            await self._clicar_botao_gerar()
            logger.info("Botão 'Gerar Relação Notas' acionado...")

            try:
                await self._page.wait_for_selector(
                    "text=Deseja Realmente Confirmar", timeout=10_000
                )
                logger.info("Modal de confirmação detectado — clicando Download...")
                await self._clicar_modal_download()
            except Exception:
                logger.info("Nenhum modal de confirmação — download direto.")

            logger.info("Aguardando download...")

        download = await dl_info.value
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        seguro = _nome_seguro(nome_cliente)
        extensao = Path(download.suggested_filename).suffix or ".xml"
        filename = f"{seguro}_{competencia_inicio.replace('/', '-')}_{ts}{extensao}"
        destino = os.path.join(pasta_destino, filename)
        await download.save_as(destino)
        logger.info(f"Arquivo salvo: {destino}")
        return destino

    async def _clicar_modal_download(self):
        """Clica no botão Download dentro do modal de confirmação."""
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
        """
        Preenche os campos de Data de Emissão com o primeiro dia do mês inicial
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

    # --------------------------------------------------------- sessão / saúde

    async def sessao_ativa(self) -> bool:
        """Verifica se a sessão está ativa pela URL atual (sem navegar).

        Retorna True se a página atual já é uma URL .jsf do portal (excluindo
        páginas de login).
        """
        url = self._page.url
        return (
            ".jsf" in url
            and "index.html" not in url
            and "login" not in url.split("/")[-1].lower()
            and url not in ("about:blank", "")
        )

    async def garantir_login(self, cnpj: str, senha: str):
        """Garante que o browser está autenticado.

        Verifica a URL atual para saber se já há sessão ativa (sem navegar).
        Se não houver, faz login.
        """
        if await self.sessao_ativa():
            return

        if self._headless:
            logger.warning(
                "Sem sessão ativa. Tentando login automático (headless). "
                "Se falhar por reCAPTCHA, rode com --visible para login manual."
            )
        else:
            logger.info("Sem sessão ativa. Abrindo browser para login manual...")
        await self.login(cnpj, senha)


# ------------------------------------------------------------------ helpers

def _limpar_cnpj(texto: str) -> str:
    return "".join(c for c in texto if c.isdigit())


def _nome_seguro(nome: str) -> str:
    return "".join(c if c.isalnum() or c in " _-" else "_" for c in nome).strip()[:50]
