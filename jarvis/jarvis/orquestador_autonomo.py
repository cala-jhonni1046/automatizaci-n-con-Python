"""Orquestador Autónomo en Segundo Plano (Arquitecto -> Ejecutor -> Auditor).

Resuelve Issues de GitHub o especificaciones en PDF conectando:
1. Búsqueda del repositorio local (.git/config) y levantado de base de datos / Docker.
2. Arquitecto en Gemini Web (Microsoft Edge con 3.1 Pro Alto o 4 Argon Medio/Bajo).
3. Ejecutor en Antigravity IDE (copia y envío del prompt técnico + monitoreo de git status / git diff).
4. Auditor en el mismo chat de Gemini Web (chat_nuevo=False) validando el git diff y generando el
   informe final estructurado en _ESTADO_TAREA_AUTONOMA.
"""
import logging
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from . import gemini_web, seguridad

try:
    import requests
except ImportError:
    requests = None

try:
    import pyperclip
except ImportError:
    pyperclip = None

log = logging.getLogger("jarvis")

_ESTADO_TAREA_AUTONOMA: dict = {
    "estado": "inactivo",  # "inactivo", "en_progreso", "completado", "error"
    "etapa_actual": "",
    "progreso": "",
    "resumen_final": "",
    "origen": "",
    "modelo": "3.1 Pro",
    "ruta_proyecto": "",
}

_PATRON_ISSUE_GITHUB = re.compile(
    r"(?:https?://)?(?:www\.)?github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/issues/(\d+)",
    re.IGNORECASE,
)
_PATRON_REPO_GITHUB = re.compile(
    r"(?:https?://)?(?:www\.)?github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)",
    re.IGNORECASE,
)
_PATRON_SLUG_REPO = re.compile(r"\b([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)\b")


def _directorios_proyectos_por_defecto() -> list[Path]:
    home = Path.home()
    candidatos = [
        Path.cwd(),
        Path.cwd().parent,
        home / "Downloads",
        home / "Projects",
        home / "Proyectos",
        home / "Documents",
        home / "Desktop",
        home / "repos",
        home / "source" / "repos",
    ]
    vistos: list[Path] = []
    for d in candidatos:
        try:
            resuelto = d.resolve()
            if resuelto.is_dir() and resuelto not in vistos:
                vistos.append(resuelto)
        except Exception:
            continue
    return vistos


def _normalizar_objetivo_repo(url_o_nombre: str) -> tuple[str, str]:
    """Extrae (usuario_repo_lower, nombre_corto_lower) a partir de una URL de GitHub, issue o nombre."""
    texto = (url_o_nombre or "").strip().strip("/")
    if not texto:
        return "", ""
    m_issue = _PATRON_ISSUE_GITHUB.search(texto)
    if m_issue:
        slug = m_issue.group(1).lower()
        return slug, slug.split("/")[-1]
    m_repo = _PATRON_REPO_GITHUB.search(texto)
    if m_repo:
        slug = m_repo.group(1).removesuffix(".git").lower()
        return slug, slug.split("/")[-1]
    texto_limpio = texto.removesuffix(".git")
    if "/" in texto_limpio and " " not in texto_limpio:
        partes = [p for p in texto_limpio.split("/") if p]
        if len(partes) >= 2:
            slug = f"{partes[-2]}/{partes[-1]}".lower()
            return slug, partes[-1].lower()
    return texto_limpio.lower(), texto_limpio.lower()


def _iterar_carpetas_hasta_nivel(base: Path, max_nivel: int = 3):
    """Recorre subcarpetas hasta max_nivel sin entrar en node_modules, .git ni entornos virtuales."""
    ignorar = {".git", "node_modules", "__pycache__", ".venv", ".venv-win", "venv", "target", "dist", "build"}
    cola: list[tuple[Path, int]] = [(base, 0)]
    while cola:
        actual, nivel = cola.pop(0)
        yield actual
        if nivel >= max_nivel:
            continue
        try:
            for hijo in actual.iterdir():
                if hijo.is_dir() and hijo.name not in ignorar and not hijo.name.startswith("."):
                    cola.append((hijo, nivel + 1))
        except OSError:
            continue


def _buscar_repositorio_local(
    url_o_nombre: str, directorios_base: list[Path] | None = None
) -> Path | None:
    """Busca en los directorios de proyectos del usuario una carpeta con .git/config que coincida."""
    entrada = (url_o_nombre or "").strip().strip('"').strip("'")
    if not entrada:
        return None

    # Si ya pasaron una ruta directa existente con .git
    try:
        ruta_directa = Path(entrada).expanduser()
        if ruta_directa.is_dir() and (ruta_directa / ".git").exists():
            return ruta_directa.resolve()
    except Exception:
        pass

    slug_objetivo, nombre_corto = _normalizar_objetivo_repo(entrada)
    if not nombre_corto:
        return None

    bases = directorios_base if directorios_base is not None else _directorios_proyectos_por_defecto()
    coincidencia_por_nombre: Path | None = None

    for base in bases:
        if not base.exists():
            continue
        for carpeta in _iterar_carpetas_hasta_nivel(base, max_nivel=3):
            git_config = carpeta / ".git" / "config"
            if not git_config.is_file():
                continue
            try:
                contenido = git_config.read_text(encoding="utf-8", errors="ignore").lower()
            except OSError:
                contenido = ""
            if slug_objetivo and slug_objetivo in contenido:
                return carpeta
            if nombre_corto and (
                f"/{nombre_corto}.git" in contenido
                or f"/{nombre_corto}\n" in contenido
                or f"/{nombre_corto} " in contenido
                or carpeta.name.lower() == nombre_corto
            ):
                if coincidencia_por_nombre is None:
                    coincidencia_por_nombre = carpeta

    return coincidencia_por_nombre


def _detectar_motor_bd(ruta_proyecto: Path) -> str:
    """Inspecciona archivos de configuración del proyecto para detectar PostgreSQL o MySQL."""
    archivos_candidatos = [
        ruta_proyecto / "docker-compose.yml",
        ruta_proyecto / "docker-compose.yaml",
        ruta_proyecto / "compose.yml",
        ruta_proyecto / "application.properties",
        ruta_proyecto / "application.yml",
        ruta_proyecto / "src" / "main" / "resources" / "application.properties",
        ruta_proyecto / "src" / "main" / "resources" / "application.yml",
        ruta_proyecto / ".env",
    ]
    texto_acumulado = []
    for archivo in archivos_candidatos:
        if archivo.is_file():
            try:
                texto_acumulado.append(archivo.read_text(encoding="utf-8", errors="ignore").lower())
            except OSError:
                continue
    unido = "\n".join(texto_acumulado)
    usa_postgres = any(p in unido for p in ("postgres", "postgresql", "5432"))
    usa_mysql = any(m in unido for m in ("mysql", "mariadb", "3306"))
    if usa_postgres and usa_mysql:
        return "PostgreSQL y MySQL"
    if usa_postgres:
        return "PostgreSQL"
    if usa_mysql:
        return "MySQL"
    return ""


def _preparar_base_datos_y_entorno(ruta_proyecto: Path) -> str:
    """Detecta PostgreSQL/MySQL en el proyecto y ejecuta docker compose up -d si corresponde."""
    if not ruta_proyecto or not ruta_proyecto.is_dir():
        return "Ruta de proyecto no válida para preparar entorno."

    motor = _detectar_motor_bd(ruta_proyecto)
    tiene_compose = any(
        (ruta_proyecto / nombre).is_file()
        for nombre in ("docker-compose.yml", "docker-compose.yaml", "compose.yml")
    )

    if not motor and not tiene_compose:
        return "No se detectaron archivos de base de datos (PostgreSQL/MySQL) ni docker-compose.yml."

    descripcion_motor = motor or "servicios de contenedores"
    if not tiene_compose:
        return f"Motor detectado: {descripcion_motor} (sin archivo docker-compose.yml local)."

    try:
        proc = subprocess.run(
            ["docker", "compose", "up", "-d"],
            cwd=str(ruta_proyecto),
            capture_output=True,
            text=True,
            timeout=60,
        )
        salida = ((proc.stdout or "") + (proc.stderr or "")).strip()
        if proc.returncode == 0:
            return f"Motor detectado: {descripcion_motor}. Entorno iniciado con 'docker compose up -d'. {salida}".strip()
        return (
            f"Motor detectado: {descripcion_motor}. 'docker compose up -d' devolvió código {proc.returncode}: "
            f"{salida}"
        )
    except FileNotFoundError:
        return f"Motor detectado: {descripcion_motor}, pero el ejecutable 'docker' no está instalado o en el PATH."
    except subprocess.TimeoutExpired:
        return f"Motor detectado: {descripcion_motor}, pero 'docker compose up -d' superó el tiempo límite."
    except Exception as e:
        return f"Motor detectado: {descripcion_motor}. Error al iniciar Docker: {e}"


def _abrir_antigravity_y_enviar_prompt(ruta_proyecto: Path, prompt_agente: str) -> str:
    """Abre Antigravity IDE en ruta_proyecto, copia prompt_agente al portapapeles y lo pega si está disponible."""
    copiado = False
    if pyperclip is not None:
        try:
            pyperclip.copy(prompt_agente or "")
            copiado = True
        except Exception:
            copiado = False

    ejecutable_ide = None
    for nombre_bin in ("antigravity", "agy", "code"):
        encontrado = shutil.which(nombre_bin)
        if encontrado:
            ejecutable_ide = encontrado
            break

    abierto = False
    if ejecutable_ide and ruta_proyecto and ruta_proyecto.exists():
        try:
            subprocess.Popen([ejecutable_ide, str(ruta_proyecto)])
            abierto = True
        except Exception as e:
            log.warning("No pude lanzar el ejecutable del IDE (%s): %s", ejecutable_ide, e)

    pegado = False
    try:
        from . import herramientas as _h
        if getattr(_h, "pywinauto", None) is not None:
            desktop = _h.pywinauto.Desktop(backend="uia")
            for ventana in desktop.windows():
                titulo = (ventana.window_text() or "").lower()
                if "antigravity" in titulo or (ruta_proyecto and ruta_proyecto.name.lower() in titulo):
                    ventana.set_focus()
                    if getattr(_h, "pyautogui", None) is not None and copiado:
                        _h.pyautogui.hotkey("ctrl", "v")
                        _h.pyautogui.press("enter")
                        pegado = True
                    break
    except Exception:
        pegado = False

    partes = []
    partes.append(f"IDE lanzado en '{ruta_proyecto}'" if abierto else f"Proyecto objetivo: '{ruta_proyecto}'")
    partes.append("prompt copiado al portapapeles" if copiado else "no se pudo copiar al portapapeles")
    if pegado:
        partes.append("y enviado automáticamente al chat del agente")
    return ", ".join(partes) + "."


def _extraer_detalle_issue_o_pdf(origen_issue_o_pdf: str) -> tuple[str, str]:
    """Obtiene (repo_referencia, detalle_texto) desde URL de GitHub Issue, ruta de PDF/archivo o texto directo."""
    origen = (origen_issue_o_pdf or "").strip()
    if not origen:
        return "", ""

    # 1) Si es una URL de un Issue de GitHub
    m_issue = _PATRON_ISSUE_GITHUB.search(origen)
    if m_issue:
        slug, numero = m_issue.group(1), m_issue.group(2)
        if requests is not None:
            try:
                resp = requests.get(
                    f"https://api.github.com/repos/{slug}/issues/{numero}",
                    headers={"Accept": "application/vnd.github+json", "User-Agent": "Jarvis-Autonomo"},
                    timeout=10,
                )
                if getattr(resp, "status_code", 500) == 200:
                    datos = resp.json()
                    titulo = datos.get("title") or f"Issue #{numero}"
                    cuerpo = datos.get("body") or "(sin descripción)"
                    return slug, f"Repositorio: {slug}\nIssue #{numero}: {titulo}\n\n{cuerpo}"
            except Exception as e:
                log.warning("No pude descargar Issue %s#%s por API: %s", slug, numero, e)
        return slug, f"Issue #{numero} del repositorio {slug} ({origen})"

    # 2) Si es un archivo local (.pdf, .md, .txt, etc.)
    try:
        posible_archivo = Path(origen.strip('"').strip("'")).expanduser()
        if posible_archivo.is_file():
            from . import herramientas as _h
            contenido_doc = _h.leer_documento_local(str(posible_archivo))
            m_repo_doc = _PATRON_REPO_GITHUB.search(contenido_doc) or _PATRON_SLUG_REPO.search(contenido_doc)
            repo_doc = m_repo_doc.group(1) if m_repo_doc else posible_archivo.stem
            return repo_doc, contenido_doc
    except Exception:
        pass

    # 3) Texto libre con posible mención de repositorio
    m_repo = _PATRON_REPO_GITHUB.search(origen) or _PATRON_SLUG_REPO.search(origen)
    repo_ref = m_repo.group(1) if m_repo else origen
    return repo_ref, origen


def _listar_estructura_proyecto(ruta_proyecto: Path | None, limite: int = 80) -> str:
    if not ruta_proyecto or not ruta_proyecto.is_dir():
        return "(proyecto local no localizado en disco)"
    ignorar = {".git", "node_modules", "__pycache__", ".venv", ".venv-win", "venv", "target", "dist", "build"}
    archivos: list[str] = []
    for carpeta in _iterar_carpetas_hasta_nivel(ruta_proyecto, max_nivel=3):
        try:
            for item in sorted(carpeta.iterdir()):
                if item.is_file() and item.name not in ignorar:
                    rel = item.relative_to(ruta_proyecto).as_posix()
                    archivos.append(rel)
                    if len(archivos) >= limite:
                        return "\n".join(archivos)
        except OSError:
            continue
    return "\n".join(archivos) if archivos else "(carpeta vacía)"


def _leer_git_status_y_diff(ruta_proyecto: Path) -> tuple[str, str]:
    """Ejecuta git status --porcelain y git diff en ruta_proyecto."""
    if not ruta_proyecto or not ruta_proyecto.is_dir():
        return "", ""
    try:
        st = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(ruta_proyecto),
            capture_output=True,
            text=True,
            timeout=15,
        )
        df = subprocess.run(
            ["git", "diff"],
            cwd=str(ruta_proyecto),
            capture_output=True,
            text=True,
            timeout=15,
        )
        return (st.stdout or "").strip(), (df.stdout or "").strip()
    except Exception:
        return "", ""


def _monitorear_cambios_git(
    ruta_proyecto: Path | None,
    timeout_segundos: float = 60.0,
    intervalo: float = 1.0,
    segundos_estabilidad: float = 2.0,
) -> str:
    """Monitorea git status --porcelain y git diff hasta que los cambios se estabilicen."""
    if not ruta_proyecto or not ruta_proyecto.is_dir():
        return "(sin ruta de repositorio local para medir git diff)"

    limite = time.monotonic() + timeout_segundos
    ultimo_estado = ""
    ultimo_diff = ""
    vio_cambios = False
    estable_desde = time.monotonic()

    while time.monotonic() < limite:
        status_actual, diff_actual = _leer_git_status_y_diff(ruta_proyecto)
        combinado = f"{status_actual}\n{diff_actual}".strip()
        if combinado:
            if not vio_cambios or combinado != f"{ultimo_estado}\n{ultimo_diff}".strip():
                vio_cambios = True
                ultimo_estado, ultimo_diff = status_actual, diff_actual
                estable_desde = time.monotonic()
            elif time.monotonic() - estable_desde >= segundos_estabilidad:
                return diff_actual or status_actual
        time.sleep(intervalo)

    if ultimo_diff or ultimo_estado:
        return ultimo_diff or ultimo_estado
    _, diff_final = _leer_git_status_y_diff(ruta_proyecto)
    return diff_final or "(no se detectaron modificaciones nuevas en git diff)"


def _ruta_modelo_orquestador(modelo: str) -> gemini_web.Ruta:
    """Construye la Ruta de Gemini Web manteniendo siempre el mismo chat activo (chat_nuevo=False)."""
    texto = (modelo or "3.1 Pro").strip()
    if "argon" in texto.lower():
        return gemini_web.Ruta(
            gemini_web.MODELO_ARGON,
            gemini_web._pensamiento_argon(texto),
            chat_nuevo=False,
        )
    if "3.1" in texto.lower() or "pro" in texto.lower():
        return gemini_web.Ruta(gemini_web.MODELO_INVESTIGACION, gemini_web.PENSAMIENTO_ALTO, chat_nuevo=False)
    return gemini_web.Ruta(gemini_web.MODELO_POR_DEFECTO, gemini_web.PENSAMIENTO_ALTO, chat_nuevo=False)


def _ejecutar_ciclo_autonomo(
    origen_issue_o_pdf: str,
    modelo: str = "3.1 Pro",
    usar_enjambre: bool = False,
    nivel_juez: str = "Alto",
) -> None:
    """Ejecuta en segundo plano el ciclo completo: Arquitecto -> Ejecutor -> Auditor."""
    from . import enjambre_web

    try:
        _ESTADO_TAREA_AUTONOMA["estado"] = "en_progreso"
        _ESTADO_TAREA_AUTONOMA["origen"] = origen_issue_o_pdf
        _ESTADO_TAREA_AUTONOMA["modelo"] = f"Enjambre 4 Argon ({nivel_juez})" if usar_enjambre else modelo
        _ESTADO_TAREA_AUTONOMA["resumen_final"] = ""

        # a) Extraer detalle del Issue/PDF y localizar el repositorio local
        _ESTADO_TAREA_AUTONOMA["etapa_actual"] = "Extrayendo especificación del Issue/PDF y buscando repositorio local"
        repo_ref, detalle_issue = _extraer_detalle_issue_o_pdf(origen_issue_o_pdf)
        ruta_proyecto = _buscar_repositorio_local(repo_ref) or _buscar_repositorio_local(origen_issue_o_pdf)
        _ESTADO_TAREA_AUTONOMA["ruta_proyecto"] = str(ruta_proyecto) if ruta_proyecto else ""
        estructura = _listar_estructura_proyecto(ruta_proyecto)
        _ESTADO_TAREA_AUTONOMA["progreso"] = (
            f"Issue leído ({len(detalle_issue)} caracteres). "
            f"Proyecto local: {ruta_proyecto or 'no encontrado'}."
        )

        # b) Arquitecto: consultar al Enjambre Multi-Cerebro o a ClienteGeminiWeb
        prompt_arquitecto = (
            "Actuá como Arquitecto de Software Senior. Analizá el siguiente Issue/especificación y la "
            "estructura del proyecto local, y redactá un PROMPT TÉCNICO DE IMPLEMENTACIÓN preciso, paso a paso, "
            "para que el agente de Antigravity IDE modifique los archivos exactos y resuelva el problema.\n\n"
            f"ESPECIFICACIÓN DEL ISSUE / TAREA:\n{detalle_issue}\n\n"
            f"ESTRUCTURA DEL PROYECTO LOCAL ({ruta_proyecto or 'sin ruta'}):\n{estructura}"
        )
        if usar_enjambre:
            _ESTADO_TAREA_AUTONOMA["etapa_actual"] = (
                f"Consultando al Enjambre Multi-Cerebro (Juez 4 Argon · {nivel_juez}) como Arquitecto"
            )
            prompt_agente = enjambre_web.consultar_enjambre_multicerebro(
                prompt_arquitecto,
                modelo_juez=gemini_web.MODELO_ARGON,
                nivel_juez=nivel_juez,
            )
        else:
            ruta_gemini = _ruta_modelo_orquestador(modelo)
            _ESTADO_TAREA_AUTONOMA["etapa_actual"] = (
                f"Consultando a Gemini ({ruta_gemini.modelo} · {ruta_gemini.pensamiento}) como Arquitecto"
            )
            prompt_agente = gemini_web.consultar(prompt_arquitecto, ruta=ruta_gemini)
        _ESTADO_TAREA_AUTONOMA["progreso"] = "Prompt técnico de implementación generado por el Arquitecto."

        # c) Levantado de base de datos/Docker y envío a Antigravity IDE
        _ESTADO_TAREA_AUTONOMA["etapa_actual"] = "Preparando Docker/Base de Datos y enviando prompt a Antigravity IDE"
        estado_entorno = (
            _preparar_base_datos_y_entorno(ruta_proyecto)
            if ruta_proyecto
            else "Sin carpeta de proyecto local para Docker."
        )
        estado_ide = _abrir_antigravity_y_enviar_prompt(ruta_proyecto or Path.cwd(), prompt_agente)
        _ESTADO_TAREA_AUTONOMA["progreso"] = f"{estado_entorno} | {estado_ide}"

        # d) Monitoreo no bloqueante de git status / git diff
        _ESTADO_TAREA_AUTONOMA["etapa_actual"] = "Monitoreando cambios del agente en git status / git diff"
        diff_resultante = _monitorear_cambios_git(ruta_proyecto)
        _ESTADO_TAREA_AUTONOMA["progreso"] = f"Cambios detectados en git ({len(diff_resultante)} caracteres)."

        # e) Auditoría en el mismo chat de Edge (chat_nuevo=False)
        prompt_auditor = (
            "Actuá como Auditor Técnico Senior en este mismo hilo. Revisá el siguiente `git diff` "
            "producido para resolver el Issue, verificá que no haya regresiones ni desequilibrios, "
            "y redactá un informe final claro estructurado exactamente con estas 4 secciones:\n"
            "1) Propósito del Issue\n"
            "2) Qué se integró exactamente\n"
            "3) Cómo funciona técnicamente\n"
            "4) Resultado de la auditoría\n\n"
            f"GIT DIFF RESULTANTE:\n```diff\n{diff_resultante[:30000]}\n```"
        )
        if usar_enjambre:
            _ESTADO_TAREA_AUTONOMA["etapa_actual"] = (
                f"Auditando git diff con el Enjambre Multi-Cerebro (Juez 4 Argon · {nivel_juez})"
            )
            informe_final = enjambre_web.consultar_enjambre_multicerebro(
                prompt_auditor,
                modelo_juez=gemini_web.MODELO_ARGON,
                nivel_juez=nivel_juez,
            )
        else:
            ruta_gemini = _ruta_modelo_orquestador(modelo)
            _ESTADO_TAREA_AUTONOMA["etapa_actual"] = (
                f"Auditando git diff en Gemini ({ruta_gemini.modelo} · {ruta_gemini.pensamiento})"
            )
            informe_final = gemini_web.consultar(prompt_auditor, ruta=ruta_gemini)
        _ESTADO_TAREA_AUTONOMA["resumen_final"] = seguridad.ocultar_secretos(informe_final)
        _ESTADO_TAREA_AUTONOMA["etapa_actual"] = "Completado"
        _ESTADO_TAREA_AUTONOMA["estado"] = "completado"
    except Exception as e:
        log.error("Error en orquestador autónomo: %s", e)
        _ESTADO_TAREA_AUTONOMA["estado"] = "error"
        _ESTADO_TAREA_AUTONOMA["etapa_actual"] = "Error durante la ejecución autónoma"
        _ESTADO_TAREA_AUTONOMA["progreso"] = seguridad.ocultar_secretos(str(e))
        _ESTADO_TAREA_AUTONOMA["resumen_final"] = (
            f"La tarea autónoma se detuvo con un error: {seguridad.ocultar_secretos(str(e))}"
        )


def iniciar_tarea_autonoma(
    origen_issue_o_pdf: str,
    modelo: str = "3.1 Pro",
    usar_enjambre: bool = False,
    nivel_juez: str = "Alto",
) -> str:
    """Lanza el ciclo autónomo en un hilo daemon y devuelve confirmación inmediata."""
    origen = (origen_issue_o_pdf or "").strip()
    if not origen:
        return "Indicá la URL del Issue de GitHub, la ruta del PDF o la descripción de la tarea."
    if _ESTADO_TAREA_AUTONOMA.get("estado") == "en_progreso":
        return (
            f"Ya hay una tarea autónoma en progreso (etapa: {_ESTADO_TAREA_AUTONOMA.get('etapa_actual')}). "
            "Podés consultarme el avance con consultar_estado_tarea_autonoma."
        )
    _ESTADO_TAREA_AUTONOMA["estado"] = "en_progreso"
    _ESTADO_TAREA_AUTONOMA["etapa_actual"] = "Iniciando hilo autónomo en segundo plano"
    _ESTADO_TAREA_AUTONOMA["progreso"] = f"Origen: {origen}"
    _ESTADO_TAREA_AUTONOMA["resumen_final"] = ""
    hilo = threading.Thread(
        target=_ejecutar_ciclo_autonomo,
        args=(origen, modelo, usar_enjambre, nivel_juez),
        name="jarvis-orquestador-autonomo",
        daemon=True,
    )
    hilo.start()
    modo_txt = f"Enjambre Multi-Cerebro (Juez 4 Argon {nivel_juez})" if usar_enjambre else modelo
    return (
        f"Listo, ya lancé en segundo plano la resolución autónoma de '{origen}' con {modo_txt}. "
        "Mientras busco el repositorio local, levanto Docker, coordino con Antigravity IDE y audito "
        "los cambios, podemos seguir charlando de cualquier otro tema."
    )


def consultar_estado_tarea() -> str:
    """Devuelve la etapa actual o el informe completo si la tarea autónoma ya finalizó."""
    estado = _ESTADO_TAREA_AUTONOMA.get("estado", "inactivo")
    etapa = _ESTADO_TAREA_AUTONOMA.get("etapa_actual", "")
    progreso = _ESTADO_TAREA_AUTONOMA.get("progreso", "")
    resumen = _ESTADO_TAREA_AUTONOMA.get("resumen_final", "")

    if estado == "inactivo":
        return "No hay ninguna tarea autónoma en ejecución ni completada en esta sesión."
    if estado == "en_progreso":
        return f"Tarea autónoma en progreso.\nEtapa actual: {etapa}\nDetalle: {progreso}"
    if estado == "error":
        return f"La tarea autónoma terminó con error.\nEtapa: {etapa}\nDetalle: {resumen or progreso}"
    return (
        f"Tarea autónoma completada ({_ESTADO_TAREA_AUTONOMA.get('modelo', '3.1 Pro')}).\n\n"
        f"{resumen}"
    )

