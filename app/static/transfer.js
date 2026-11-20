/* 积分转赠界面：全部操作走 JSON API，失败时展示阻断原因且不改动本地余额视图。 */
const result = document.querySelector('#result');
const setResult = (text, bad) => { result.textContent = text; result.style.color = bad ? 'var(--bad)' : 'var(--ok)'; };

async function call(url, body) {
  const resp = await fetch(url, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const data = await resp.json();
  if (!resp.ok) {
    const msg = data.detail || data.code || '请求失败';
    const err = new Error(msg);
    err.payload = data;
    throw err;
  }
  return data;
}

function selectRow(row) {
  document.querySelectorAll('.order-row').forEach(r => r.classList.remove('selected'));
  row.classList.add('selected');
  document.querySelector('#a-transfer').value = row.dataset.id;
  document.querySelector('#a-actor').value = '';
  document.querySelector('#selection').textContent =
    `已选 ${row.dataset.id}：${row.dataset.sender} → ${row.dataset.recipient}（${row.dataset.status}）`;
}
document.querySelectorAll('.order-row').forEach(row =>
  row.addEventListener('click', () => selectRow(row)));

document.querySelector('#btn-open').addEventListener('click', async () => {
  const transfer_id = document.querySelector('#f-transfer').value.trim();
  const sender = document.querySelector('#f-sender').value.trim();
  const recipient = document.querySelector('#f-recipient').value.trim();
  const amount = Number(document.querySelector('#f-amount').value);
  if (!transfer_id || !sender || !recipient || !amount) { setResult('请完整填写转赠单与双方会员、积分', true); return; }
  try {
    const data = await call('/api/transfer/open', { transfer_id, sender, recipient, amount });
    if (data.blocked) {
      setResult(`幂等命中：${data.detail}，未新增额度`, true);
    } else {
      setResult(`已托管 ${data.order.amount} 分：${data.order.transfer_id} 待领取`, false);
    }
    setTimeout(() => location.reload(), 600);
  } catch (e) {
    setResult(`提交被阻断：${e.message}（旧版余额未改动）`, true);
  }
});

document.querySelector('#btn-claim').addEventListener('click', async () => {
  const transfer_id = document.querySelector('#a-transfer').value.trim();
  const actor = document.querySelector('#a-actor').value.trim();
  if (!transfer_id || !actor) { setResult('请先选择转赠单并填写领取会员号', true); return; }
  try {
    const data = await call('/api/transfer/claim', { transfer_id, actor });
    if (data.blocked) { setResult(`领取阻断：${data.detail}`, true); }
    else { setResult(`领取成功：${transfer_id} 已划转，接收方当前可用 ${data.balances.recipient.available}`, false); }
    setTimeout(() => location.reload(), 700);
  } catch (e) { setResult(`领取失败：${e.message}`, true); }
});

document.querySelector('#btn-revoke').addEventListener('click', async () => {
  const transfer_id = document.querySelector('#a-transfer').value.trim();
  const actor = document.querySelector('#a-actor').value.trim();
  if (!transfer_id || !actor) { setResult('请先选择转赠单并填写撤回会员号', true); return; }
  try {
    const data = await call('/api/transfer/revoke', { transfer_id, actor });
    if (data.blocked) { setResult(`撤回阻断：${data.detail}`, true); }
    else { setResult(`撤回成功：${transfer_id} 原路退回，转出方当前可用 ${data.balances.sender.available}`, false); }
    setTimeout(() => location.reload(), 700);
  } catch (e) { setResult(`撤回失败：${e.message}`, true); }
});

document.querySelector('#btn-sweep').addEventListener('click', async () => {
  try {
    await call('/api/transfer/sweep', {});
    setResult('到期清扫完成', false);
    setTimeout(() => location.reload(), 600);
  } catch (e) { setResult(`清扫失败：${e.message}`, true); }
});

document.querySelector('#refresh').addEventListener('click', () => location.reload());
