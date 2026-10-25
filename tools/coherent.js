// Evaluate JavaScript in the running sim's UI views through the Coherent GT inspector, which
// the sim serves on 127.0.0.1:19999 in Developer Mode. Used to explore UI data while
// developing patches.
//
//   node tools/coherent.js list
//   node tools/coherent.js eval <view title/url substring> "<expression>"
//
// The expression may return a promise; its resolved value is printed as JSON.
const BASE = process.env.COHERENT_URL || 'http://127.0.0.1:19999';

async function targets() {
    // Coherent GT serves a WebKit-style inspector: pages at /pagelist.json, each inspected
    // over ws://<host>/devtools/page/<id>.
    const res = await fetch(BASE + '/pagelist.json');
    const host = new URL(BASE).host;
    return (await res.json()).map((p) => ({
        id: String(p.id),
        title: p.title,
        url: p.url,
        webSocketDebuggerUrl: `ws://${host}/devtools/page/${p.id}`,
    }));
}

function evaluate(wsUrl, expression) {
    return new Promise((resolve, reject) => {
        const ws = new WebSocket(wsUrl);
        let nextId = 1;
        const pending = new Map();
        const send = (method, params) => new Promise((res) => {
            const id = nextId++;
            pending.set(id, res);
            ws.send(JSON.stringify({ id, method, params }));
        });
        const timer = setTimeout(() => { ws.close(); reject(new Error('timed out')); }, 15000);
        ws.onerror = (e) => { clearTimeout(timer); reject(new Error('websocket error: ' + (e.message || e.type))); };
        ws.onmessage = (msg) => {
            const data = JSON.parse(msg.data);
            if (data.id && pending.has(data.id)) {
                pending.get(data.id)(data);
                pending.delete(data.id);
            }
        };
        ws.onopen = async () => {
            // The WebKit protocol can't await promises, so the page resolves the expression,
            // parks the result as JSON on window, and we poll for it.
            const key = '__noWhooshEval' + Date.now();
            const wrapped = `(async () => { try { const v = await (${expression}); `
                + `return JSON.stringify({ ok: true, value: v === undefined ? null : v }, null, 2); } `
                + `catch (e) { return JSON.stringify({ ok: false, error: String(e && e.stack || e) }); } })()`
                + `.then(r => { window.${key} = r; })`;
            let reply = await send('Runtime.evaluate', { expression: wrapped });
            if (reply.error || (reply.result && reply.result.wasThrown)) {
                clearTimeout(timer);
                ws.close();
                resolve(reply);
                return;
            }
            for (let i = 0; i < 140; i++) {
                reply = await send('Runtime.evaluate', { expression: `window.${key}`, returnByValue: true });
                if (reply.result && reply.result.result && reply.result.result.type === 'string') {
                    break;
                }
                await new Promise((r) => setTimeout(r, 100));
            }
            await send('Runtime.evaluate', { expression: `delete window.${key}` });
            clearTimeout(timer);
            ws.close();
            resolve(reply);
        };
    });
}

async function main() {
    const [cmd, match, expression] = process.argv.slice(2);
    const list = await targets();
    if (cmd === 'list') {
        for (const t of list) {
            console.log(`${t.id}\t${t.title || ''}\t${t.url || ''}`);
        }
        return;
    }
    if (cmd !== 'eval' || !match || !expression) {
        throw new Error('usage: coherent.js list | eval <view substring> "<expression>"');
    }
    const target = list.find((t) => (t.title || '').includes(match) || (t.url || '').includes(match));
    if (!target) {
        throw new Error('no view matching ' + match);
    }
    const reply = await evaluate(target.webSocketDebuggerUrl, expression);
    const result = reply.result && reply.result.result;
    if (result && typeof result.value === 'string') {
        console.log(result.value);
    } else {
        console.log(JSON.stringify(reply, null, 2));
    }
}

main().catch((e) => { console.error(e.message); process.exit(1); });
