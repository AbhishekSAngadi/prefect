"""
Functions for interacting with block data ORM objects.
Intended for internal use by the Orion API.
"""
import json
import os
from uuid import UUID

import pendulum
import sqlalchemy as sa
from cryptography.fernet import Fernet

from prefect.orion import schemas
from prefect.orion.database.dependencies import inject_db
from prefect.orion.database.interface import OrionDBInterface
from prefect.orion.models import configurations


@inject_db
async def create_block_data(
    session: sa.orm.Session,
    block_data: schemas.core.BlockData,
    db: OrionDBInterface,
):
    insert_values = block_data.dict(shallow=True, exclude_unset=False)
    insert_values.pop("created")
    insert_values.pop("updated")
    blockname = insert_values["name"]

    insert_values["data"] = await encrypt_blockdata(session, insert_values["data"])

    insert_stmt = (await db.insert(db.BlockData)).values(**insert_values)

    await session.execute(insert_stmt)
    query = (
        sa.select(db.BlockData)
        .where(db.BlockData.name == blockname)
        .execution_options(populate_existing=True)
    )

    result = await session.execute(query)
    return result.scalar()


@inject_db
async def read_block_data_as_block(
    session: sa.orm.Session,
    block_data_id: UUID,
    db: OrionDBInterface,
):
    query = (
        sa.select(db.BlockData)
        .where(db.BlockData.id == block_data_id)
        .with_for_update()
    )

    result = await session.execute(query)
    blockdata = result.scalar()

    if not blockdata:
        return None

    blockdata_dict = {
        "name": blockdata.name,
        "blockref": blockdata.blockref,
        "blockid": blockdata.id,
        "data": blockdata.data,
    }

    blockdata_dict["data"] = await decrypt_blockdata(session, blockdata_dict["data"])
    return unpack_blockdata(blockdata_dict)


@inject_db
async def read_block_data_by_name_as_block(
    session: sa.orm.Session,
    name: str,
    db: OrionDBInterface,
):
    query = sa.select(db.BlockData).where(db.BlockData.name == name)
    result = await session.execute(query)
    blockdata = result.scalar()

    if not blockdata:
        return None

    blockdata_dict = {
        "name": blockdata.name,
        "blockref": blockdata.blockref,
        "blockid": blockdata.id,
        "data": blockdata.data,
    }

    blockdata_dict["data"] = await decrypt_blockdata(session, blockdata_dict["data"])
    return unpack_blockdata(blockdata_dict)


@inject_db
async def delete_block_data_by_name(
    session: sa.orm.Session,
    name: str,
    db: OrionDBInterface,
) -> bool:

    query = sa.delete(db.BlockData).where(db.BlockData.name == name)

    result = await session.execute(query)
    return result.rowcount > 0


@inject_db
async def update_block_data(
    session: sa.orm.Session,
    name: str,
    block_data: schemas.actions.BlockDataUpdate,
    db: OrionDBInterface,
) -> bool:

    update_values = block_data.dict(shallow=True, exclude_unset=True)
    update_values = {k: v for k, v in update_values.items() if v is not None}
    if "data" in update_values:
        update_values["data"] = await encrypt_blockdata(session, update_values["data"])

    update_stmt = (
        sa.update(db.BlockData).where(db.BlockData.name == name).values(update_values)
    )

    result = await session.execute(update_stmt)
    return result.rowcount > 0


def pack_blockdata(raw_block):
    blockdata = dict()
    blockdata["name"] = raw_block.pop("blockname")
    blockdata["blockref"] = raw_block.pop("blockref")

    # we remove blockid here in the event that a Block schema was used to template
    # a block, the id will be generated by the ORM model on write
    raw_block.pop("blockid", None)

    blockdata["data"] = raw_block
    return blockdata


def unpack_blockdata(blockdata):
    block = dict(**blockdata["data"])
    block["blockname"] = blockdata.pop("name", None)
    block["blockref"] = blockdata.pop("blockref", None)
    block["blockid"] = blockdata.pop("blockid", None)

    return block


async def get_fernet_encryption(session):
    environment_key = os.getenv("ORION_BLOCK_ENCRYPTION_KEY")
    if environment_key:
        return Fernet(environment_key.encode())

    configured_key = await configurations.read_configuration_by_key(
        session, "BLOCK_ENCRYPTION_KEY"
    )

    if configured_key is None:
        encryption_key = Fernet.generate_key()
        configured_key = schemas.core.Configuration(
            key="BLOCK_ENCRYPTION_KEY", value={"fernet_key": encryption_key.decode()}
        )
        await configurations.create_configuration(session, configured_key)
    else:
        encryption_key = configured_key.value["fernet_key"].encode()
    return Fernet(encryption_key)


async def encrypt_blockdata(session, blockdata: dict):
    fernet = await get_fernet_encryption(session)
    byte_blob = json.dumps(blockdata).encode()
    return {"encrypted_blob": fernet.encrypt(byte_blob).decode()}


async def decrypt_blockdata(session, blockdata: dict):
    fernet = await get_fernet_encryption(session)
    byte_blob = blockdata["encrypted_blob"].encode()
    return json.loads(fernet.decrypt(byte_blob).decode())
