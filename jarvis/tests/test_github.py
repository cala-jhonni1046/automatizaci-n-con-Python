"""Pruebas unitarias con mocks para el módulo de GitHub en jarvis/herramientas.py."""
import datetime
from types import SimpleNamespace

import pytest

from jarvis import config, herramientas, seguridad


@pytest.fixture
def repo_falso(monkeypatch):
    """Configura un GITHUB_TOKEN de prueba y un cliente Github simulado."""
    monkeypatch.setattr(config, "GITHUB_TOKEN", "ghp_tokenDePrueba12345")

    arbol_items = [
        SimpleNamespace(path="README.md", type="blob"),
        SimpleNamespace(path="jarvis", type="tree"),
        SimpleNamespace(path="jarvis/main.py", type="blob"),
    ]

    issue_real = SimpleNamespace(
        number=12,
        title="Error al iniciar micrófono",
        user=SimpleNamespace(login="ana"),
        labels=[SimpleNamespace(name="bug"), SimpleNamespace(name="voz")],
        body="Cuando no hay auriculares puestos tira un warning.",
        pull_request=None,
    )
    # En la API de GitHub, get_issues también devuelve PRs (tienen pull_request != None); se deben filtrar.
    pr_como_issue = SimpleNamespace(
        number=15,
        title="PR disfrazado",
        user=SimpleNamespace(login="pedro"),
        labels=[],
        body="No debe salir en issues.",
        pull_request=SimpleNamespace(url="https://api.github.com/repos/u/r/pulls/15"),
    )

    pr_activo = SimpleNamespace(
        number=15,
        title="Agrega soporte para GitHub",
        user=SimpleNamespace(login="pedro"),
        head=SimpleNamespace(ref="feat/github"),
        base=SimpleNamespace(ref="main"),
        get_files=lambda: [
            SimpleNamespace(filename="jarvis/herramientas.py"),
            SimpleNamespace(filename="tests/test_github.py"),
        ],
    )

    commits = [
        SimpleNamespace(
            sha="a1b2c3d4e5f67890",
            author=SimpleNamespace(login="jhonn"),
            commit=SimpleNamespace(
                author=SimpleNamespace(
                    name="Jhonn",
                    date=datetime.datetime(2026, 10, 29, 12, 0),
                ),
                message="Implementa herramientas de GitHub\n\nDetalle extra del commit.",
            ),
        ),
        SimpleNamespace(
            sha="9988776655443322",
            author=SimpleNamespace(login="ana"),
            commit=SimpleNamespace(
                author=SimpleNamespace(
                    name="Ana",
                    date=datetime.datetime(2026, 10, 28, 18, 30),
                ),
                message="Optimiza vision.py",
            ),
        ),
    ]

    class RepoSimulado:
        default_branch = "main"
        ultima_rama_arbol = None
        ultima_ref_archivo = None

        def get_git_tree(self, sha, recursive=False):
            assert recursive is True
            self.ultima_rama_arbol = sha
            if sha == "rama-inexistente":
                raise herramientas.GithubException(404, "Not Found")
            return SimpleNamespace(tree=arbol_items)

        def get_contents(self, ruta, ref=None):
            self.ultima_ref_archivo = ref
            if ruta == "no_existe.py":
                raise herramientas.GithubException(404, "Not Found")
            if ruta == "jarvis":
                return [SimpleNamespace(path="jarvis/main.py"), SimpleNamespace(path="jarvis/config.py")]
            return SimpleNamespace(decoded_content=f"# Contenido de {ruta}\nprint('hola')\n".encode("utf-8"))

        def get_issues(self, state="open"):
            if state == "closed":
                return []
            return [issue_real, pr_como_issue]

        def get_pulls(self, state="open"):
            if state == "closed":
                return []
            return [pr_activo]

        def get_commits(self):
            return commits

    instancia_repo = RepoSimulado()

    class ClienteGithubSimulado:
        def __init__(self, token):
            self.token = token

        def get_repo(self, nombre):
            if nombre == "usuario/no-existe":
                raise herramientas.GithubException(404, "Not Found")
            return instancia_repo

    monkeypatch.setattr(herramientas, "Github", ClienteGithubSimulado)
    return instancia_repo


@pytest.mark.parametrize("entrada, esperado", [
    ("usuario/repo", "usuario/repo"),
    ("https://github.com/usuario/repo", "usuario/repo"),
    ("https://github.com/usuario/repo/", "usuario/repo"),
    ("https://github.com/usuario/repo.git", "usuario/repo"),
    ("https://www.github.com/usuario/repo/tree/main/src", "usuario/repo"),
    ("github.com/mi-org/mi_repo.py", "mi-org/mi_repo.py"),
    ("solo-una-palabra", ""),
    ("", ""),
])
def test_limpiar_repo(entrada, esperado):
    assert herramientas._limpiar_repo(entrada) == esperado


def test_gh_sin_token_devuelve_mensaje_claro(monkeypatch):
    monkeypatch.setattr(config, "GITHUB_TOKEN", None)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    for nombre, args in [
        ("gh_estructura_repo", {"repositorio": "usuario/repo"}),
        ("gh_leer_archivo", {"repositorio": "usuario/repo", "ruta_archivo": "README.md"}),
        ("gh_listar_issues", {"repositorio": "usuario/repo"}),
        ("gh_ver_pull_requests", {"repositorio": "usuario/repo"}),
        ("gh_ultimos_commits", {"repositorio": "usuario/repo"}),
    ]:
        salida = herramientas.ejecutar(nombre, args)
        assert "GITHUB_TOKEN" in salida
        assert ".env" in salida


def test_gh_repo_inexistente_o_formato_invalido(repo_falso):
    invalido = herramientas.ejecutar("gh_estructura_repo", {"repositorio": "url-rota"})
    assert "no es válido" in invalido

    no_existe = herramientas.ejecutar("gh_estructura_repo", {"repositorio": "https://github.com/usuario/no-existe"})
    assert "No encontré el repositorio 'usuario/no-existe'" in no_existe


def test_gh_estructura_repo_usa_default_branch_y_rama_explicita(repo_falso):
    salida = herramientas.ejecutar("gh_estructura_repo", {"repositorio": "https://github.com/usuario/repo"})
    assert repo_falso.ultima_rama_arbol == "main"
    assert "README.md" in salida
    assert "jarvis/" in salida
    assert "jarvis/main.py" in salida

    salida_rama = herramientas.ejecutar("gh_estructura_repo", {"repositorio": "usuario/repo", "rama": "develop"})
    assert repo_falso.ultima_rama_arbol == "develop"
    assert "rama develop" in salida_rama

    error_rama = herramientas.ejecutar("gh_estructura_repo", {"repositorio": "usuario/repo", "rama": "rama-inexistente"})
    assert "No encontré la rama 'rama-inexistente'" in error_rama


def test_gh_leer_archivo_texto_y_errores(repo_falso):
    contenido = herramientas.ejecutar(
        "gh_leer_archivo",
        {"repositorio": "https://github.com/usuario/repo", "ruta_archivo": "jarvis/main.py", "rama": "dev"},
    )
    assert repo_falso.ultima_ref_archivo == "dev"
    assert "# Contenido de jarvis/main.py" in contenido
    assert "print('hola')" in contenido

    carpeta = herramientas.ejecutar("gh_leer_archivo", {"repositorio": "usuario/repo", "ruta_archivo": "jarvis"})
    assert "es una carpeta" in carpeta

    faltante = herramientas.ejecutar("gh_leer_archivo", {"repositorio": "usuario/repo", "ruta_archivo": "no_existe.py"})
    assert "No encontré el archivo 'no_existe.py'" in faltante


def test_gh_listar_issues_filtra_prs_y_muestra_detalles(repo_falso):
    salida = herramientas.ejecutar("gh_listar_issues", {"repositorio": "usuario/repo"})
    assert "#12 - Error al iniciar micrófono" in salida
    assert "ana" in salida
    assert "bug, voz" in salida
    assert "auriculares" in salida
    assert "PR disfrazado" not in salida

    cerrados = herramientas.ejecutar("gh_listar_issues", {"repositorio": "usuario/repo", "estado": "closed"})
    assert "No hay issues" in cerrados


def test_gh_ver_pull_requests_muestra_ramas_y_archivos(repo_falso):
    salida = herramientas.ejecutar("gh_ver_pull_requests", {"repositorio": "https://github.com/usuario/repo"})
    assert "PR #15 - Agrega soporte para GitHub" in salida
    assert "pedro" in salida
    assert "feat/github -> main" in salida
    assert "jarvis/herramientas.py" in salida
    assert "tests/test_github.py" in salida

    cerrados = herramientas.ejecutar("gh_ver_pull_requests", {"repositorio": "usuario/repo", "estado": "closed"})
    assert "No hay pull requests" in cerrados


def test_gh_ultimos_commits_respeta_cantidad_y_formato(repo_falso):
    salida = herramientas.ejecutar("gh_ultimos_commits", {"repositorio": "usuario/repo", "cantidad": 1})
    assert "[a1b2c3d]" in salida
    assert "2026-10-29 12:00" in salida
    assert "Jhonn" in salida
    assert "Implementa herramientas de GitHub" in salida
    assert "9988776" not in salida


def test_ocultar_secretos_protege_tokens_de_github():
    texto = "Token filtrado: ghp_abc123XYZ987 y GITHUB_TOKEN=secreto_super_privado"
    limpio = seguridad.ocultar_secretos(texto)
    assert "ghp_abc123XYZ987" not in limpio
    assert "secreto_super_privado" not in limpio
    assert "ghp_***" in limpio

