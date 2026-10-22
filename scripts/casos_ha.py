#!/usr/bin/env python3
"""Casos de prueba de H&A contra el Nea real (fork H&A).

El Laboratorio de Vocero trae clientes simulados fijos de una ferretería
(taladros, martillos…), así que no sirve para probar a Sofía. Este guion hace
lo mismo que el Laboratorio pero con clientes de inmobiliaria:

1. Crea en la base del CRM un contacto archivado y una conversación de prueba
   (`is_test = true`): el CRM GUARDA las respuestas y jamás las envía a
   WhatsApp (sandbox de la 033).
2. Por cada línea del cliente inserta el mensaje entrante y le entrega a Nea
   un despacho firmado, igual al del CRM.
3. Espera la respuesta (o el traspaso a humano), imprime la conversación con
   unas revisiones automáticas, y al final borra la conversación y el contacto.

Corre DENTRO del contenedor de Nea, que ya tiene DATABASE_URL y
CRM_BOT_API_KEY:

    python scripts/casos_ha.py            # los 10 casos
    python scripts/casos_ha.py 4 9        # solo los casos 4 y 9

Gasta tokens de la llave de IA del CRM (unas decenas de turnos por corrida).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import asyncpg
import httpx

NEA_URL = os.environ.get("NEA_URL", "http://localhost:8000")
TIMEOUT_TURNO = 120.0  # s esperando la respuesta de un turno
QUIETUD = 4.0  # s extra tras la primera respuesta: un turno puede mandar varias


@dataclass
class Caso:
    numero: int
    nombre: str
    espera: str  # lo que debería pasar, en palabras
    guion: list[str]
    debe_escalar: bool | None = None  # None = no se revisa
    no_debe_contener: list[str] = field(default_factory=list)
    debe_contener_alguno: list[str] = field(default_factory=list)


CASOS = [
    Caso(1, "Compra que califica",
         "Busca inmuebles, muestra opciones reales con enlace y ofrece agendar.",
         ["Hola, buenas tardes",
          "Quiero comprar un apartamento en el sur de Cali",
          "Tengo 300 millones, con crédito preaprobado",
          "Pienso comprar en unos 3 meses",
          "Me interesa el primero, ¿lo puedo ver?"],
         debe_contener_alguno=["bienesraicesha.com/propiedad"]),
    Caso(2, "Compra por debajo",
         "No miente sobre el precio: dice con tacto que queda por encima y ofrece similares.",
         ["Hola",
          "Quiero comprar, vi un apartamento de 320 millones en el sur",
          "Tengo 220 millones",
          "En 3 meses más o menos"]),
    Caso(3, "Compra muy por debajo",
         "Si no hay nada en su rango, se despide con amabilidad (salida digna) sin inventar.",
         ["Buenas",
          "Quiero comprar una casa en Cali",
          "Tengo 40 millones",
          "Para este mes"]),
    Caso(4, "Arriendo justo",
         "Califica con póliza y ofrece la visita o muestra opciones reales.",
         ["Hola, busco apartamento en arriendo",
          "En Valle del Lili",
          "Tengo 2 millones 500 para el canon",
          "Me mudo este mes",
          "Tengo póliza de arrendamiento"],
         debe_contener_alguno=["bienesraicesha.com/propiedad"]),
    Caso(5, "Arriendo bajo",
         "Presupuesto muy bajo: salida digna, nunca mezcla con venta.",
         ["Hola, quiero arrendar un apartamento",
          "En el norte de Cali",
          "Tengo 600 mil pesos",
          "Ya mismo"],
         no_debe_contener=["en venta"]),
    Caso(6, "Solo mirando",
         "No lo descarta: responde y ofrece la visita una sola vez, sin presionar.",
         ["Hola",
          "Quiero comprar apartamento",
          "Unos 350 millones",
          "La verdad solo estoy mirando opciones por ahora",
          "Gracias, lo voy a pensar"]),
    Caso(7, "Negociador",
         "No negocia: pasa a una persona.",
         ["Hola, vi un apartamento de ustedes en venta",
          "¿Me lo dejan en 280 millones? Pago de contado"],
         debe_escalar=True,
         no_debe_contener=["descuento del", "se lo dejamos"]),
    Caso(8, "Propietario",
         "Es captación: pasa a una persona.",
         ["Buenas tardes",
          "Quiero arrendar mi apartamento con ustedes, ¿cómo funciona?"],
         debe_escalar=True),
    Caso(9, "Pregunta sin dato",
         "No inventa la administración: ofrece confirmarla con el asesor.",
         ["Hola",
          "Busco apartamento en arriendo en Valle del Lili por 2.700.000",
          "¿Cuánto es la administración del primero?"],
         no_debe_contener=["la administración es de", "administración de $"]),
    Caso(10, "Cliente molesto",
         "Se disculpa breve y pasa a una persona.",
         ["Llevo 3 días esperando que me llamen, qué falta de seriedad",
          "Quiero que me atienda alguien ya"],
         debe_escalar=True),
]


def firmar(cuerpo: bytes, secreto: str) -> str:
    return "sha256=" + hmac.new(secreto.encode(), cuerpo, hashlib.sha256).hexdigest()


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def nid(prefijo: str) -> str:
    return f"{prefijo}_ha{secrets.token_hex(8)}"


async def correr_caso(db: asyncpg.Connection, org: asyncpg.Record, caso: Caso,
                      llave: str, cli: httpx.AsyncClient) -> dict:
    # Teléfono sintético único por corrida: Nea no recuerda casos anteriores.
    tel = "5799" + str(int(time.time()))[-6:] + f"{caso.numero:02d}"
    contacto, conv = nid("contact"), nid("conversation")
    await db.execute(
        "insert into contact (id, organization_id, phone, wa_identity, name, archived_at)"
        " values ($1,$2,$3,$3,$4, now())",
        contacto, org["id"], tel, f"[Prueba H&A] {caso.nombre}")
    await db.execute(
        "insert into conversation (id, organization_id, contact_id, is_test, ai_enabled)"
        " values ($1,$2,$3,true,true)", conv, org["id"], contacto)

    falla = None
    try:
        for linea in caso.guion:
            ahora = datetime.now(timezone.utc)
            ahora_db = ahora.replace(tzinfo=None)  # columnas `timestamp` sin zona, en UTC
            msg = nid("message")
            await db.execute(
                "insert into message (id, organization_id, conversation_id, direction, type,"
                " text, status, wa_timestamp) values ($1,$2,$3,'in','text',$4,'delivered',$5)",
                msg, org["id"], conv, linea, ahora_db)
            await db.execute(
                "update conversation set last_inbound_at=$2, last_message_at=$2, updated_at=$2"
                " where id=$1", conv, ahora_db)
            evento = {
                "dispatchId": nid("dsp"),
                "organization": {"id": org["id"], "slug": org["slug"]},
                "conversation": {"id": conv, "channel": "whatsapp", "aiEnabled": True,
                                 "windowOpen": True,
                                 "windowExpiresAt": iso(ahora + timedelta(hours=24)),
                                 "test": True},
                "contact": {"id": contacto, "displayName": f"[Prueba H&A] {caso.nombre}",
                            "identity": tel},
                "messages": [{"id": msg, "at": iso(ahora), "type": "text", "text": linea,
                              "mediaId": None}],
            }
            cuerpo = json.dumps(evento).encode()
            r = await cli.post(f"{NEA_URL}/vocero/dispatch", content=cuerpo,
                               headers={"x-vocero-signature": firmar(cuerpo, llave),
                                        "content-type": "application/json"})
            if r.status_code >= 300:
                falla = f"Nea rechazó el despacho: HTTP {r.status_code} {r.text[:200]}"
                break
            limite = time.monotonic() + TIMEOUT_TURNO
            contesto = False
            while time.monotonic() < limite:
                await asyncio.sleep(1.5)
                fila = await db.fetchrow(
                    "select (select count(*) from message where conversation_id=$1"
                    " and direction='out' and created_at > $2) as nuevas,"
                    " (select handoff_at from conversation where id=$1) as handoff",
                    conv, ahora_db)
                if fila["nuevas"] or fila["handoff"]:
                    contesto = True
                    await asyncio.sleep(QUIETUD)
                    break
            if not contesto:
                falla = f"sin respuesta en {TIMEOUT_TURNO:.0f} s a: «{linea}»"
                break
            if (await db.fetchval("select handoff_at from conversation where id=$1", conv)):
                break  # como el Laboratorio: el primer traspaso termina el guion

        mensajes = await db.fetch(
            "select direction, text from message where conversation_id=$1 and text is not null"
            " order by created_at", conv)
        handoff = await db.fetchrow(
            "select handoff_at, handoff_reason from conversation where id=$1", conv)
    finally:
        await db.execute("delete from conversation where id=$1", conv)
        await db.execute("delete from contact where id=$1", contacto)

    texto_agente = " ".join(m["text"] for m in mensajes if m["direction"] == "out").lower()
    revisiones = []
    if falla:
        revisiones.append(("❌", falla))
    escalo = bool(handoff and handoff["handoff_at"])
    if caso.debe_escalar is not None:
        ok = escalo == caso.debe_escalar
        revisiones.append(("✅" if ok else "❌",
                           f"{'pasó' if escalo else 'no pasó'} a una persona"
                           f" (se esperaba que {'sí' if caso.debe_escalar else 'no'})"))
    for t in caso.no_debe_contener:
        if t.lower() in texto_agente:
            revisiones.append(("❌", f"dijo «{t}»"))
    if caso.debe_contener_alguno:
        ok = any(t.lower() in texto_agente for t in caso.debe_contener_alguno)
        revisiones.append(("✅" if ok else "⚠️",
                           "mostró inmuebles reales con enlace" if ok
                           else "no mostró ningún inmueble con enlace"))
    return {"caso": caso, "mensajes": mensajes, "escalo": escalo,
            "motivo": handoff["handoff_reason"] if handoff else None, "revisiones": revisiones}


def imprimir(res: dict) -> None:
    c: Caso = res["caso"]
    print(f"\n{'=' * 70}\nCASO {c.numero} · {c.nombre}\nSe espera: {c.espera}\n{'-' * 70}")
    for m in res["mensajes"]:
        quien = "CLIENTE" if m["direction"] == "in" else "SOFÍA  "
        print(f"{quien} │ {m['text']}")
    if res["escalo"]:
        print(f"SISTEMA │ pasó a una persona (motivo: {res['motivo'] or 'sin motivo'})")
    print("-" * 70)
    for icono, texto in res["revisiones"] or [("👀", "sin revisión automática: léelo")]:
        print(f"{icono} {texto}")


async def main() -> int:
    elegidos = {int(a) for a in sys.argv[1:]} or {c.numero for c in CASOS}
    llave = os.environ["CRM_BOT_API_KEY"]
    db = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        org = await db.fetchrow(
            "select o.id, o.slug from organization o join agent_profile p"
            " on p.organization_id = o.id limit 1")
        resultados = []
        async with httpx.AsyncClient(timeout=30) as cli:
            for caso in CASOS:
                if caso.numero in elegidos:
                    res = await correr_caso(db, org, caso, llave, cli)
                    imprimir(res)
                    resultados.append(res)
    finally:
        await db.close()
    fallas = sum(1 for r in resultados for i, _ in r["revisiones"] if i == "❌")
    print(f"\n{'=' * 70}\nResumen: {len(resultados)} casos, {fallas} revisiones en ❌.")
    print("Las revisiones automáticas son una ayuda: lee cada conversación.")
    return 1 if fallas else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
