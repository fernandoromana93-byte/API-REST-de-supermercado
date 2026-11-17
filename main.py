import io
import os
import sqlite3
import unicodedata
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

SEED = [
    ("Leche entera 1L", "Lácteos", 4200, 60),
    ("Huevos AA x30", "Despensa", 21500, 25),
    ("Arroz Diana 500g", "Despensa", 3100, 120),
    ("Aceite de girasol 1L", "Despensa", 14900, 8),
    ("Manzana roja kg", "Frutas y verduras", 7800, 40),
    ("Pan tajado", "Panadería", 6500, 0),
    ("Queso campesino 500g", "Lácteos", 12900, 15),
    ("Detergente líquido 2L", "Aseo", 24900, 30),
]


def norm(s: str) -> str:
    """Minúsculas y sin tildes, para comparar nombres ('CAFÉ' == 'cafe')."""
    return "".join(c for c in unicodedata.normalize("NFD", s) if not unicodedata.combining(c)).casefold()


@contextmanager
def db():
    con = sqlite3.connect(os.getenv("DB_PATH", "supermercado.db"))
    con.row_factory = sqlite3.Row
    con.create_function("norm", 1, norm, deterministic=True)
    try:
        yield con
        con.commit()
    finally:
        con.close()


@asynccontextmanager
async def lifespan(_):
    with db() as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS productos ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "nombre TEXT NOT NULL UNIQUE, "
            "categoria TEXT NOT NULL, "
            "precio REAL NOT NULL CHECK(precio > 0), "
            "stock INTEGER NOT NULL CHECK(stock >= 0))"
        )
        if not c.execute("SELECT 1 FROM productos").fetchone():
            c.executemany("INSERT INTO productos(nombre, categoria, precio, stock) VALUES (?,?,?,?)", SEED)
    yield


app = FastAPI(
    title="API Supermercado",
    version="1.0.0",
    description="Gestión de productos de un supermercado. Las respuestas incluyen enlaces `_links` (HATEOAS).",
    lifespan=lifespan,
)


# ---------- Modelos ----------
class ProductoIn(BaseModel):
    nombre: str = Field(min_length=1, max_length=100, examples=["Leche deslactosada 1L"])
    categoria: str = Field(min_length=1, max_length=50, examples=["Lácteos"])
    precio: float = Field(gt=0, examples=[4800])
    stock: int = Field(ge=0, examples=[50])


class Producto(ProductoIn):
    id: int
    links: dict = Field(alias="_links", description="Enlaces HATEOAS del recurso")


class Coleccion(BaseModel):
    total: int
    items: list[Producto]
    links: dict = Field(alias="_links", description="Enlaces HATEOAS de la colección")


# ---------- HATEOAS ----------
def url(req: Request, name: str, **params) -> str:
    return str(req.url_for(name, **params))


def recurso(fila, req: Request) -> dict:
    return {
        **dict(fila),
        "_links": {"self": {"href": url(req, "obtener", id=fila["id"])}, "todos": {"href": url(req, "listar")}},
    }


def coleccion(filas, req: Request) -> dict:
    return {
        "total": len(filas),
        "items": [recurso(f, req) for f in filas],
        "_links": {
            "self": {"href": str(req.url)},
            "todos": {"href": url(req, "listar")},
            "buscar": {"href": url(req, "buscar") + "?nombre={nombre}", "templated": True},
            "crear": {"href": url(req, "listar"), "method": "POST"},
            "reporte": {"href": url(req, "reporte"), "type": "application/pdf"},
        },
    }


# ---------- Endpoints ----------
@app.get("/productos", response_model=Coleccion, tags=["Productos"], name="listar")
def listar(req: Request):
    """Consulta general: productos con stock disponible."""
    with db() as c:
        return coleccion(c.execute("SELECT * FROM productos WHERE stock > 0 ORDER BY nombre").fetchall(), req)


@app.get("/productos/buscar", response_model=Coleccion, tags=["Productos"], name="buscar")
def buscar(req: Request, nombre: str = Query(min_length=1, description="Texto contenido en el nombre")):
    """Consulta por nombre (coincidencia parcial, sin distinguir mayúsculas)."""
    with db() as c:
        filas = c.execute("SELECT * FROM productos WHERE norm(nombre) LIKE ? ORDER BY nombre", (f"%{norm(nombre)}%",)).fetchall()
    return coleccion(filas, req)


@app.get("/productos/{id}", response_model=Producto, tags=["Productos"], name="obtener")
def obtener(id: int, req: Request):
    """Detalle de un producto (destino del enlace `self`)."""
    with db() as c:
        fila = c.execute("SELECT * FROM productos WHERE id = ?", (id,)).fetchone()
    if not fila:
        raise HTTPException(404, "Producto no encontrado")
    return recurso(fila, req)


@app.post("/productos", response_model=Producto, status_code=201, tags=["Productos"], name="crear")
def crear(p: ProductoIn, req: Request, res: Response):
    """Ingresa un producto nuevo. El nombre debe ser único."""
    with db() as c:
        if c.execute("SELECT 1 FROM productos WHERE norm(nombre) = ?", (norm(p.nombre.strip()),)).fetchone():
            raise HTTPException(409, "Ya existe un producto con ese nombre")
        id = c.execute(
            "INSERT INTO productos(nombre, categoria, precio, stock) VALUES (?,?,?,?)",
            (p.nombre.strip(), p.categoria.strip(), p.precio, p.stock),
        ).lastrowid
        fila = c.execute("SELECT * FROM productos WHERE id = ?", (id,)).fetchone()
    res.headers["Location"] = url(req, "obtener", id=id)
    return recurso(fila, req)


@app.get(
    "/reportes/productos.pdf",
    tags=["Reportes"],
    name="reporte",
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}}, "description": "Reporte de inventario en PDF"}},
)
def reporte():
    """Reporte básico de inventario: totales y listado por categoría."""
    with db() as c:
        filas = c.execute("SELECT * FROM productos ORDER BY categoria, nombre").fetchall()
    estilos = getSampleStyleSheet()
    valor = sum(f["precio"] * f["stock"] for f in filas)
    sin_stock = sum(1 for f in filas if f["stock"] == 0)
    tabla = Table(
        [["ID", "Producto", "Categoría", "Precio", "Stock"]]
        + [[f["id"], f["nombre"], f["categoria"], f"${f['precio']:,.0f}", f["stock"]] for f in filas],
        repeatRows=1,
        colWidths=[35, 190, 120, 70, 45],
    )
    tabla.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#14213D")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#EEF2F4")]),
                ("ALIGN", (3, 0), (-1, -1), "RIGHT"),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
            ]
        )
    )
    buf = io.BytesIO()
    SimpleDocTemplate(buf, pagesize=A4).build(
        [
            Paragraph("Reporte de inventario", estilos["Title"]),
            Paragraph(
                f"Productos: {len(filas)} &nbsp;|&nbsp; Sin stock: {sin_stock} &nbsp;|&nbsp; "
                f"Valor del inventario: ${valor:,.0f}",
                estilos["Normal"],
            ),
            Spacer(1, 14),
            tabla,
        ]
    )
    return Response(
        buf.getvalue(),
        media_type="application/pdf",
        headers={"Content-Disposition": 'inline; filename="reporte_productos.pdf"'},
    )


app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


@app.get("/", include_in_schema=False)
def inicio():
    return FileResponse(Path(__file__).parent / "static" / "index.html")
