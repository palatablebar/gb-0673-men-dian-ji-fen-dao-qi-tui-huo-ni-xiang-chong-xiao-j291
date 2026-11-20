"""积分转赠 M8：页面与 JSON API。"""
from fastapi import APIRouter, Request, Body
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from app.config import ROOT, META, DB_PATH
from app.errors import BusinessError
from app.modules.transfer.service import TransferService, BLOCK_REASONS
from app.modules.transfer.seeder import seed_if_empty
from app.transfer_store import SqliteTransferRepository

router = APIRouter()
templates = Jinja2Templates(directory=str(ROOT / 'app/templates'))


def service():
    seed_if_empty(DB_PATH)
    return TransferService(SqliteTransferRepository(DB_PATH))


@router.get('/transfer')
def page(request: Request):
    dash = service().dashboard()
    return templates.TemplateResponse(
        request=request, name='transfer.html',
        context={'meta': META, 'dash': dash, 'reasons': BLOCK_REASONS})


@router.get('/api/transfer/dashboard')
def dashboard():
    return service().dashboard()


@router.post('/api/transfer/open')
def open_transfer(payload: dict = Body(...)):
    transfer_id = str(payload.get('transfer_id', '')).strip()
    if not transfer_id:
        raise BusinessError('BAD_TRANSFER_ID', '转赠编号不能为空')
    result = service().open_transfer(
        transfer_id, payload.get('sender'), payload.get('recipient'),
        payload.get('amount'), idempotency_key=transfer_id)
    return JSONResponse(result, status_code=200 if result['ok'] else 409)


@router.post('/api/transfer/claim')
def claim(payload: dict = Body(...)):
    if not payload.get('transfer_id') or not payload.get('actor'):
        raise BusinessError('BAD_PAYLOAD', 'transfer_id 与 actor 均为必填')
    result = service().claim(str(payload['transfer_id']), str(payload['actor']))
    return JSONResponse(result, status_code=200 if result['ok'] else 409)


@router.post('/api/transfer/revoke')
def revoke(payload: dict = Body(...)):
    if not payload.get('transfer_id') or not payload.get('actor'):
        raise BusinessError('BAD_PAYLOAD', 'transfer_id 与 actor 均为必填')
    result = service().revoke(str(payload['transfer_id']), str(payload['actor']))
    return JSONResponse(result, status_code=200 if result['ok'] else 409)


@router.post('/api/transfer/sweep')
def sweep():
    return service().sweep_expired()


@router.get('/api/transfer/orders/{transfer_id}')
def order_detail(transfer_id: str):
    return service().get_order(transfer_id)
