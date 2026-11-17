import re

import pytest
from fastapi.testclient import TestClient

from main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


NUEVO = {"nombre": "Café molido 250g", "categoria": "Despensa", "precio": 9800, "stock": 20}


# 1. Consulta general
def test_listar_solo_disponibles(client):
    d = client.get("/productos").json()
    assert d["total"] == len(d["items"]) > 0
    assert all(p["stock"] > 0 for p in d["items"])
    assert "Pan tajado" not in [p["nombre"] for p in d["items"]]  # stock 0 en la semilla


# 2. Consulta por nombre
def test_buscar_parcial_sin_mayusculas(client):
    d = client.get("/productos/buscar", params={"nombre": "LECHE"}).json()
    assert [p["nombre"] for p in d["items"]] == ["Leche entera 1L"]


def test_buscar_incluye_agotados(client):
    assert client.get("/productos/buscar", params={"nombre": "pan"}).json()["total"] == 1


def test_buscar_sin_resultados_devuelve_lista_vacia(client):
    r = client.get("/productos/buscar", params={"nombre": "zzz"})
    assert r.status_code == 200 and r.json()["items"] == []


def test_buscar_sin_parametro_es_422(client):
    assert client.get("/productos/buscar").status_code == 422


# 3. Ingreso de productos
def test_crear_producto(client):
    r = client.post("/productos", json=NUEVO)
    assert r.status_code == 201
    p = r.json()
    assert p["id"] and p["nombre"] == NUEVO["nombre"]
    assert r.headers["location"].endswith(f"/productos/{p['id']}")
    for q in ("café", "CAFE", "molido"):  # sin importar mayúsculas ni tildes
        assert client.get("/productos/buscar", params={"nombre": q}).json()["total"] == 1


@pytest.mark.parametrize("nombre", ["Café molido 250g", "CAFÉ MOLIDO 250G", " cafe molido 250g "])
def test_crear_duplicado_es_409(client, nombre):
    client.post("/productos", json=NUEVO)
    assert client.post("/productos", json={**NUEVO, "nombre": nombre}).status_code == 409


@pytest.mark.parametrize(
    "cambio",
    [{"precio": 0}, {"precio": -5}, {"stock": -1}, {"nombre": ""}, {"categoria": ""}, {"precio": "abc"}],
)
def test_crear_invalido_es_422(client, cambio):
    assert client.post("/productos", json={**NUEVO, **cambio}).status_code == 422


def test_crear_sin_campos_es_422(client):
    assert client.post("/productos", json={}).status_code == 422


def test_obtener_existente_e_inexistente(client):
    assert client.get("/productos/1").status_code == 200
    assert client.get("/productos/9999").status_code == 404


# 4. Reporte PDF
def test_reporte_pdf(client):
    client.post("/productos", json=NUEVO)
    r = client.get("/reportes/productos.pdf")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF") and r.content.rstrip().endswith(b"%%EOF")


# HATEOAS: se puede navegar solo siguiendo enlaces
def test_hateoas_navegacion_por_enlaces(client):
    links = client.get("/productos").json()["_links"]
    assert {"self", "todos", "buscar", "crear", "reporte"} <= links.keys()
    assert client.get(links["todos"]["href"]).status_code == 200
    assert client.get(links["reporte"]["href"]).status_code == 200

    url_buscar = links["buscar"]["href"].replace("{nombre}", "arroz")
    assert client.get(url_buscar).json()["total"] == 1

    nuevo = client.request(links["crear"]["method"], links["crear"]["href"], json=NUEVO).json()
    assert client.get(nuevo["_links"]["self"]["href"]).json()["nombre"] == NUEVO["nombre"]


def test_hateoas_cada_item_tiene_self(client):
    for p in client.get("/productos").json()["items"]:
        assert client.get(p["_links"]["self"]["href"]).json()["id"] == p["id"]


# Documentación y frontend
def test_openapi_documenta_los_endpoints(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert "get" in paths["/productos"] and "post" in paths["/productos"]
    assert "/productos/buscar" in paths and "/reportes/productos.pdf" in paths
    assert client.get("/docs").status_code == 200


def test_frontend_servido_y_consume_la_api(client):
    r = client.get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert re.search(r'cargar\("/productos"', r.text)
