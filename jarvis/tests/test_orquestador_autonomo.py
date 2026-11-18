"""Tests unitarios para el Orquestador Autónomo en Segundo Plano (orquestador_autonomo.py)."""
from pathlib import Path
from types import SimpleNamespace

import pytest

import vision
from jarvis import gemini_web, herramientas, orquestador_autonomo


@pytest.fixture(autouse=True)
def limpiar_estado_autonomo():
    orquestador_autonomo._ESTADO_TAREA_AUTONOMA.update({
        "estado": "inactivo",
        "etapa_actual": "",
        "progreso": "",
        "resumen_final": "",
        "origen": "",
        "modelo": "3.1 Pro",
        "ruta_proyecto": "",
    })
    yield


# ---------------------------------------------------------------- 1. Búsqueda de repositorio local

def test_buscar_repositorio_local_por_url_y_nombre_en_git_config(tmp_path):
    proyectos = tmp_path / "proyectos"
    repo_dir = proyectos / "sistema-clinico"
    git_dir = repo_dir / ".git"
    git_dir.mkdir(parents=True)
    (git_dir / "config").write_text(
        '[remote "origin"]\n\turl = https://github.com/jhonny/sistema-clinico.git\n',
        encoding="utf-8",
    )

    encontrado_url = orquestador_autonomo._buscar_repositorio_local(
        "https://github.com/jhonny/sistema-clinico/issues/15",
        directorios_base=[proyectos],
    )
    assert encontrado_url == repo_dir

    encontrado_nombre = orquestador_autonomo._buscar_repositorio_local(
        "sistema-clinico",
        directorios_base=[proyectos],
    )
    assert encontrado_nombre == repo_dir


# ---------------------------------------------------------------- 2. Levantado de BD y Docker

def test_preparar_base_datos_y_entorno_detecta_postgresql_y_ejecuta_docker_compose(monkeypatch, tmp_path):
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n  db:\n    image: postgres:16\n    ports:\n      - '5432:5432'\n",
        encoding="utf-8",
    )
    comandos = []

    def falso_run(cmd, cwd=None, capture_output=True, text=True, timeout=60):
        comandos.append((cmd, cwd))
        return SimpleNamespace(returncode=0, stdout="Container db Started", stderr="")

    monkeypatch.setattr(orquestador_autonomo.subprocess, "run", falso_run)

    resultado = orquestador_autonomo._preparar_base_datos_y_entorno(tmp_path)
    assert "PostgreSQL" in resultado
    assert "docker compose up -d" in resultado
    assert comandos == [(["docker", "compose", "up", "-d"], str(tmp_path))]


def test_preparar_base_datos_detecta_mysql_en_application_properties(tmp_path):
    recursos = tmp_path / "src" / "main" / "resources"
    recursos.mkdir(parents=True)
    (recursos / "application.properties").write_text(
        "spring.datasource.url=jdbc:mysql://localhost:3306/app\n",
        encoding="utf-8",
    )
    resultado = orquestador_autonomo._preparar_base_datos_y_entorno(tmp_path)
    assert "MySQL" in resultado


# ---------------------------------------------------------------- 3. Antigravity IDE + Portapapeles

def test_abrir_antigravity_y_enviar_prompt_copia_al_portapapeles_y_abre_ide(monkeypatch, tmp_path):
    copiados = []
    lanzados = []

    monkeypatch.setattr(
        orquestador_autonomo,
        "pyperclip",
        SimpleNamespace(copy=lambda txt: copiados.append(txt)),
    )
    monkeypatch.setattr(orquestador_autonomo.shutil, "which", lambda b: "C:/bin/agy.exe" if b == "agy" else None)
    monkeypatch.setattr(orquestador_autonomo.subprocess, "Popen", lambda cmd: lanzados.append(cmd))

    estado = orquestador_autonomo._abrir_antigravity_y_enviar_prompt(tmp_path, "Implementar endpoint /salud")
    assert copiados == ["Implementar endpoint /salud"]
    assert lanzados == [["C:/bin/agy.exe", str(tmp_path)]]
    assert "prompt copiado al portapapeles" in estado


# ---------------------------------------------------------------- 4. Ciclo Arquitecto -> Ejecutor -> Auditor

def test_ciclo_autonomo_completo_arquitecto_ejecutor_auditor(monkeypatch, tmp_path):
    repo_dir = tmp_path / "mi-repo"
    (repo_dir / ".git").mkdir(parents=True)
    (repo_dir / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = https://github.com/org/mi-repo.git\n',
        encoding="utf-8",
    )
    (repo_dir / "main.py").write_text("print('hola')\n", encoding="utf-8")

    consultas_gemini = []

    def falso_consultar_gemini(mensaje, ruta=None, imagen=None, timeout=0):
        consultas_gemini.append((mensaje, ruta))
        if len(consultas_gemini) == 1:
            return "PROMPT PARA ANTIGRAVITY: Editar main.py y agregar validación."
        return (
            "1) Propósito del Issue: Validar entrada.\n"
            "2) Qué se integró exactamente: Función validar() en main.py.\n"
            "3) Cómo funciona técnicamente: Verifica tipos antes de procesar.\n"
            "4) Resultado de la auditoría: Aprobado sin regresiones."
        )

    monkeypatch.setattr(gemini_web, "consultar", falso_consultar_gemini)
    monkeypatch.setattr(
        orquestador_autonomo,
        "_buscar_repositorio_local",
        lambda ref, directorios_base=None: repo_dir,
    )
    monkeypatch.setattr(
        orquestador_autonomo,
        "_preparar_base_datos_y_entorno",
        lambda r: "Motor detectado: PostgreSQL. Contenedores activos.",
    )
    monkeypatch.setattr(
        orquestador_autonomo,
        "_abrir_antigravity_y_enviar_prompt",
        lambda r, p: "IDE lanzado, prompt copiado.",
    )
    monkeypatch.setattr(
        orquestador_autonomo,
        "_monitorear_cambios_git",
        lambda r, **kw: "diff --git a/main.py b/main.py\n+def validar(): pass",
    )

    orquestador_autonomo._ejecutar_ciclo_autonomo(
        "https://github.com/org/mi-repo/issues/7",
        modelo="4 Argon",
    )

    assert len(consultas_gemini) == 2
    # Arquitecto y Auditor usan el mismo chat (chat_nuevo=False) y respetan 4 Argon en Medio
    _, ruta_arq = consultas_gemini[0]
    msg_aud, ruta_aud = consultas_gemini[1]
    assert ruta_arq.modelo == "4 Argon"
    assert ruta_arq.pensamiento == "Medio"
    assert ruta_arq.chat_nuevo is False
    assert ruta_aud.modelo == "4 Argon"
    assert ruta_aud.pensamiento == "Medio"
    assert ruta_aud.chat_nuevo is False
    assert "+def validar(): pass" in msg_aud

    estado_final = herramientas.consultar_estado_tarea_autonoma()
    assert "Tarea autónoma completada" in estado_final
    assert "1) Propósito del Issue" in estado_final
    assert "4) Resultado de la auditoría: Aprobado sin regresiones." in estado_final


# ---------------------------------------------------------------- 5. Herramientas no bloqueantes

def test_iniciar_tarea_autonoma_issue_retorna_inmediato_y_es_no_bloqueante(monkeypatch):
    hilos_iniciados = []

    class HiloFalso:
        def __init__(self, target=None, args=(), name=None, daemon=None):
            self.target = target
            self.args = args
            self.daemon = daemon

        def start(self):
            hilos_iniciados.append((self.args, self.daemon))

    monkeypatch.setattr(orquestador_autonomo.threading, "Thread", HiloFalso)

    msg = herramientas.iniciar_tarea_autonoma_issue(
        "https://github.com/org/proyecto/issues/12",
        modelo="3.1 Pro",
    )
    assert "segundo plano" in msg
    assert hilos_iniciados == [(("https://github.com/org/proyecto/issues/12", "3.1 Pro", False, "Alto"), True)]

    estado_en_curso = herramientas.consultar_estado_tarea_autonoma()
    assert "en progreso" in estado_en_curso

    assert "iniciar_tarea_autonoma_issue" in vision.NO_BLOQUEANTES
    assert "consultar_estado_tarea_autonoma" in vision.NO_BLOQUEANTES

