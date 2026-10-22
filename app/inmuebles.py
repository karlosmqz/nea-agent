"""Inventario de inmuebles (fork H&A): el agente solo ofrece lo que hay.

La fuente es el XML del catálogo de Meta que genera reha desde WASI
(`INMUEBLES_FEED_URL`). Se lee al arrancar y luego cada
`INMUEBLES_REFRESH_SECONDS` (24 h por defecto). Si una descarga falla se
conserva el último inventario bueno: un Drive caído no puede dejar al agente
sin inmuebles ni, peor, ofreciendo una lista vacía como si no hubiera nada.

El XML no trae tipo ni área como campos: el tipo sale del título de WASI
(«APARTAMENTO EN ALQUILER…», «VENTA CASA…») y habitaciones/baños de las
etiquetas `product_tags` (`2_habitaciones`, `2_banos`).

Reglas de calificación (decisión de Carlos, 22-oct-2026), venta y arriendo
nunca se mezclan:
- venta:    presupuesto ≥ 85% del precio califica · 60–85% similares · < 60% nada
- arriendo: presupuesto ≥ 90% del canon califica  · 75–90% similares · < 75% nada
"""
from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger("nea.inmuebles")

UMBRALES = {"venta": (0.85, 0.60), "arriendo": (0.90, 0.75)}
MAX_RESULTADOS = 3
MAX_FOTOS = 5

# Orden importa: lo más específico primero («casa campestre» antes que «casa»,
# «apartaestudio» antes que «apartamento»).
TIPOS = [
    ("apartaestudio", ("apartaestudio",)),
    ("casa campestre", ("casa campestre", "finca")),
    ("casa lote", ("casa lote",)),
    ("apartamento", ("apartamento", "apartamentno", "pent house", "penthouse")),
    ("casa", ("casa",)),
    ("lote", ("lote",)),
    ("local", ("local",)),
    ("oficina", ("oficina",)),
    ("bodega", ("bodega",)),
    ("edificio", ("edificio",)),
]


def _sin_tildes(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", texto or "")
    return "".join(c for c in texto if not unicodedata.combining(c)).lower().strip()


def tipo_desde_titulo(titulo: str) -> str:
    t = _sin_tildes(titulo)
    for tipo, claves in TIPOS:
        if any(c in t for c in claves):
            return tipo
    return "otro"


@dataclass
class Inmueble:
    id: str
    titulo: str
    operacion: str  # venta | arriendo
    tipo: str
    barrio: str
    ciudad: str
    precio: int
    habitaciones: int | None
    banos: int | None
    estrato: int | None
    url: str
    fotos: list[str] = field(default_factory=list)

    def para_llm(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "titulo": self.titulo,
            "operacion": self.operacion,
            "tipo": self.tipo,
            "barrio": self.barrio,
            "ciudad": self.ciudad,
            "precio_cop": self.precio,
            "habitaciones": self.habitaciones,
            "banos": self.banos,
            "estrato": self.estrato,
            "url": self.url,
            "foto": self.fotos[0] if self.fotos else None,
        }


def _numero_de_etiqueta(tags: list[str], sufijo: str) -> int | None:
    for tag in tags:
        m = re.fullmatch(r">?(\d+)_" + sufijo, tag)
        if m:
            return int(m.group(1))
    return None


def parse_feed(xml: bytes) -> list[Inmueble]:
    raiz = ET.fromstring(xml)
    out: list[Inmueble] = []
    for x in raiz.iter("listing"):
        disponible = (x.findtext("availability") or "").strip()
        if disponible not in ("for_sale", "for_rent"):
            continue
        precio_txt = (x.findtext("price") or "").split()
        try:
            precio = int(float(precio_txt[0])) if precio_txt else 0
        except ValueError:
            precio = 0
        if precio <= 0:
            continue  # sin precio no se puede calificar a nadie contra él
        tags = [(t.text or "").strip() for t in x.findall("product_tags")]
        ciudad = ""
        addr = x.find("address")
        if addr is not None:
            for comp in addr:
                if comp.attrib.get("name") == "city":
                    ciudad = (comp.text or "").strip()
        estrato = next(
            (int(t.split("_")[1]) for t in tags if re.fullmatch(r"estrato_\d+", t)), None
        )
        titulo = re.sub(r"\s+", " ", (x.findtext("name") or "").replace("\xa0", " ")).strip()
        fotos: list[str] = []
        for img in x.findall("image"):
            u = (img.findtext("url") or "").strip()
            if u and u not in fotos:
                fotos.append(u)
        out.append(
            Inmueble(
                id=(x.findtext("home_listing_id") or "").strip(),
                titulo=titulo,
                operacion="venta" if disponible == "for_sale" else "arriendo",
                tipo=tipo_desde_titulo(titulo),
                barrio=(x.findtext("neighborhood") or "").strip(),
                ciudad=ciudad,
                precio=precio,
                habitaciones=_numero_de_etiqueta(tags, "habitaciones"),
                banos=_numero_de_etiqueta(tags, "banos"),
                estrato=estrato,
                url=(x.findtext("url") or "").strip(),
                fotos=fotos[:MAX_FOTOS],
            )
        )
    return out


def _coincide_zona(inm: Inmueble, zona: str) -> bool:
    z = _sin_tildes(zona)
    if not z:
        return False
    lugar = _sin_tildes(f"{inm.barrio} {inm.ciudad} {inm.titulo}")
    return all(p in lugar for p in z.split())


class Inventario:
    def __init__(self, items: list[Inmueble] | None = None) -> None:
        self.items: list[Inmueble] = list(items or [])
        self.actualizado: datetime | None = None

    def __len__(self) -> int:
        return len(self.items)

    def por_id(self, listing_id: str) -> Inmueble | None:
        return next((i for i in self.items if i.id == str(listing_id)), None)

    def buscar(
        self,
        operacion: str,
        presupuesto: int | None = None,
        tipo: str | None = None,
        zona: str | None = None,
        habitaciones_min: int | None = None,
    ) -> dict[str, Any]:
        operacion = _sin_tildes(operacion)
        if operacion in ("compra", "comprar", "vender"):
            operacion = "venta"
        if operacion in ("arrendar", "alquiler", "renta"):
            operacion = "arriendo"
        if operacion not in UMBRALES:
            return {"ok": False, "error": "operacion debe ser 'venta' o 'arriendo'"}

        candidatos = [i for i in self.items if i.operacion == operacion]
        if tipo:
            t = tipo_desde_titulo(tipo)
            candidatos = [i for i in candidatos if i.tipo == t]
        if habitaciones_min:
            candidatos = [
                i for i in candidatos if i.habitaciones is None or i.habitaciones >= habitaciones_min
            ]

        def orden(i: Inmueble) -> tuple[int, int]:
            # primero los de su zona, luego el precio más cercano a su presupuesto
            cerca = 0 if zona and _coincide_zona(i, zona) else 1
            return (cerca, abs(i.precio - (presupuesto or i.precio)))

        califica, minimo = UMBRALES[operacion]
        if presupuesto:
            dentro = [i for i in candidatos if presupuesto >= califica * i.precio]
            cercanos = [
                i for i in candidatos if minimo * i.precio <= presupuesto < califica * i.precio
            ]
        else:
            dentro, cercanos = candidatos, []

        if dentro:
            elegidos, resultado = sorted(dentro, key=orden)[:MAX_RESULTADOS], "califica"
        elif cercanos:
            elegidos, resultado = sorted(cercanos, key=orden)[:MAX_RESULTADOS], "por_encima_del_presupuesto"
        else:
            elegidos, resultado = [], "sin_resultados"

        return {
            "ok": True,
            "resultado": resultado,
            "inmuebles": [i.para_llm() for i in elegidos],
            "nota": (
                "Muestra SOLO estos inmuebles, con su url. Precio sujeto a confirmación del asesor. "
                "Si resultado es 'por_encima_del_presupuesto', dilo con tacto: superan un poco su presupuesto. "
                "Si es 'sin_resultados', no inventes: ofrece que un asesor lo contacte (handoff)."
            ),
        }


BUSCAR_INMUEBLES_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "buscar_inmuebles",
        "description": (
            "Busca en el inventario REAL de H&A. Úsala cuando ya sepas si compra o arrienda "
            "(y idealmente presupuesto, tipo y zona). Nunca describas, prometas ni inventes un "
            "inmueble que esta herramienta no haya devuelto."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "operacion": {"type": "string", "enum": ["venta", "arriendo"]},
                "presupuesto": {
                    "type": "integer",
                    "description": "Presupuesto del cliente en pesos colombianos (canon mensual si arrienda)",
                },
                "tipo": {"type": "string", "description": "apartamento, casa, apartaestudio, lote, local, oficina…"},
                "zona": {"type": "string", "description": "Barrio, sector o ciudad que pidió el cliente"},
                "habitaciones_min": {"type": "integer"},
            },
            "required": ["operacion"],
        },
    },
}


async def descargar(url: str, timeout: float = 60.0) -> list[Inmueble]:
    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as cli:
        r = await cli.get(url)
        r.raise_for_status()
    return parse_feed(r.content)


async def mantener_inventario(inv: Inventario, url: str, cada_segundos: float) -> None:
    """Worker: refresca el inventario; un fallo deja el último bueno."""
    while True:
        try:
            nuevos = await descargar(url)
            if nuevos:
                inv.items = nuevos
                inv.actualizado = datetime.now(timezone.utc)
                logger.info("inventario: %d inmuebles cargados", len(nuevos))
            else:
                logger.warning("inventario: el feed vino vacío; se conserva el anterior (%d)", len(inv))
        except Exception as exc:  # noqa: BLE001 — el worker nunca muere
            logger.warning("inventario: no se pudo descargar el feed: %s", exc)
        await asyncio.sleep(cada_segundos)
