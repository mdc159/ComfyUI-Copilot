import { useEffect, useState } from 'react';
import Modal from '../ui/Modal';
type Choice = {connection: string; model: string};
type Connection = {id: string; name: string; provider: string; mode: string; env?: string; base_url?: string; credential_source: string};
type Settings = {connections: Connection[]; chat: Choice; workflow: Choice | null};
type Model = {id: string; name: string};
type Login = {id: string; status: string; error?: string; events: {message?: string; instructions?: string; url?: string; userCode?: string; verificationUri?: string; links?: {url:string;label?:string}[]}[]; prompt?: {id:string; type:string; message:string; options?: {id:string; label:string}[]}};
async function api(path = '', body?: unknown) {
    const response = await fetch('/api/copilot/llm' + path, body ? {method:'POST', headers:{'Content-Type':'application/json','X-Copilot-Settings':'1'}, body:JSON.stringify(body)} : undefined);
    const result = await response.json();
    if (!response.ok || result.error) throw new Error(result.error || 'Model connection request failed');
    return result;
}
export function ApiKeyModal({isOpen, onClose, onConfigurationUpdated}: {isOpen:boolean; onClose:()=>void; onConfigurationUpdated?:()=>void}) {
    const [settings, setSettings] = useState<Settings|null>(null);
    const [editing, setEditing] = useState<Connection|null>(null);
    const [secret, setSecret] = useState('');
    const [models, setModels] = useState<Record<string, Model[]>>({});
    const [search, setSearch] = useState('');
    const [message, setMessage] = useState('');
    const [busy, setBusy] = useState(false);
    const [login, setLogin] = useState<Login|null>(null);
    const [reply, setReply] = useState('');
    const refresh = async () => setSettings(await api());
    const run = async (action:()=>Promise<void>) => {
        setBusy(true); setMessage('');
        try { await action(); } catch(e) { setMessage(e instanceof Error ? e.message : 'Connection failed'); }
        finally { setBusy(false); }
    };
    const load = async (id:string) => {
        const result = await api('/models?connection=' + encodeURIComponent(id));
        setModels(old => ({...old, [id]:result.models})); return result.models as Model[];
    };
    useEffect(() => { if (isOpen) { void run(refresh); setEditing(null); setSecret(''); } }, [isOpen]);
    useEffect(() => {
        if (!isOpen || login?.status !== 'pending') return;
        const timer = setInterval(() => { api('/login/' + login.id).then(async state => {
            setLogin(state); if (state.status === 'connected') { await refresh(); setMessage('Sign-in saved securely. Choose and test a model below.'); }
        }).catch(e => setMessage(e.message)); }, 1200);
        return () => clearInterval(timer);
    }, [isOpen, login?.id, login?.status]);
    const pickConnection = async (role:'chat'|'workflow', id:string) => {
        if (!settings) return;
        if (!id && role === 'workflow') { setSettings({...settings, workflow:null}); return; }
        setSettings({...settings, [role]:{connection:id,model:''}});
        const rows = await load(id);
        setSettings(old => old ? {...old, [role]:{connection:id,model:rows[0]?.id || ''}} : old);
    };
    const picker = (role:'chat'|'workflow') => {
        if (!settings) return null;
        const choice = settings[role]; const rows = choice ? models[choice.connection] || [] : [];
        return <fieldset className="border rounded p-3 min-w-0"><legend className="px-1 font-medium">{role === 'chat' ? 'Chat model' : 'Workflow and debug model'}</legend>
            <label htmlFor={role+'-connection'} className="block text-xs">Connection</label>
            <select id={role+'-connection'} className="w-full border rounded p-2 bg-white text-gray-900" disabled={busy} value={choice?.connection || ''} onChange={e=>void run(()=>pickConnection(role,e.target.value))}>
                {role === 'workflow' && <option value="">Use chat connection and model</option>}
                {settings.connections.map(c=><option key={c.id} value={c.id}>{c.name}</option>)}
            </select>
            {choice && <><label htmlFor={role+'-model'} className="block text-xs mt-3">Model</label>
                <select id={role+'-model'} className="w-full border rounded p-2 bg-white text-gray-900" value={choice.model} onChange={e=>setSettings({...settings,[role]:{...choice,model:e.target.value}})}>
                    <option value="">Choose a model</option>
                    {!rows.some(m=>m.id===choice.model) && choice.model && <option value={choice.model}>{choice.model}</option>}
                    {rows.filter(m=>(m.name+' '+m.id).toLowerCase().includes(search.toLowerCase())).map(m=><option key={m.id} value={m.id}>{m.name}</option>)}
                </select><div className="flex gap-2 mt-2"><button disabled={busy} onClick={()=>void run(async()=>{await load(choice.connection);setMessage('Catalog loaded. Test the selected model to verify access.');})}>Refresh models</button>
                <button disabled={busy || !choice.model} onClick={()=>void run(async()=>{const result=await api('',{action:'test',connection:choice.connection,model:choice.model});setMessage('Model connection verified: '+result.reply);})}>Test model</button></div></>}
        </fieldset>;
    };
    return <Modal open={isOpen} onClose={onClose} autoClose={false} className="copilot-model-settings w-[760px] max-h-[90vh] overflow-y-auto text-gray-900">
        <h2 className="text-xl font-semibold mb-2">Models &amp; connections</h2>
        <p className="text-sm text-gray-600 mb-3">API keys, provider sign-ins, and local models. Subscription connections never fall back to API billing.</p>
        {message && <p role="status" className="border rounded p-3 my-3 whitespace-pre-wrap">{message}</p>}
        {!settings ? <p>Loading connections…</p> : <>
            <div className="divide-y">{settings.connections.map(c=><div key={c.id} className="flex flex-wrap items-center justify-between gap-2 py-2">
                <div><strong className="text-sm">{c.name}</strong><div className="text-xs text-gray-500">{c.credential_source} · {c.mode==='oauth'?'provider sign-in':c.mode==='local'?c.base_url:'API billing'}</div></div>
                <div className="flex gap-2">{c.mode==='oauth'?<button disabled={busy || login?.status==='pending'} onClick={()=>void run(async()=>setLogin(await api('',{action:'login',connection:c.id})))}>Sign in</button>:<button disabled={busy} onClick={()=>{setEditing({...c});setSecret('');}}>Configure</button>}
                {c.credential_source==='encrypted store' && <button disabled={busy} onClick={()=>void run(async()=>{await api('',{action:'disconnect',connection:c.id});await refresh();})}>Remove saved credential</button>}</div>
            </div>)}</div>
            <button className="mt-3" disabled={busy} onClick={()=>{setEditing({id:'',name:'Custom endpoint',provider:'custom',mode:'api',env:'',base_url:'http://127.0.0.1:1234/v1',credential_source:''});setSecret('');}}>Add custom endpoint</button>
            {editing && <fieldset className="border rounded p-3 my-3 space-y-2"><legend>Configure {editing.name}</legend>
                <label className="block">Name<input className="block border rounded p-2 w-full" value={editing.name} onChange={e=>setEditing({...editing,name:e.target.value})}/></label>
                {editing.provider==='custom' && <label className="block">Base URL<input className="block border rounded p-2 w-full" value={editing.base_url || ''} onChange={e=>setEditing({...editing,base_url:e.target.value})}/></label>}
                <label className="block">Environment variable / Latchkey key name<input className="block border rounded p-2 w-full" value={editing.env || ''} onChange={e=>setEditing({...editing,env:e.target.value})}/></label>
                <label className="block">API key (optional; stored encrypted)<input className="block border rounded p-2 w-full" type="password" autoComplete="off" value={secret} onChange={e=>setSecret(e.target.value)}/></label>
                <p className="text-xs text-gray-500">A blank key keeps the existing credential. Local servers usually need no key.</p>
                <button disabled={busy} onClick={()=>void run(async()=>{await api('',{action:'connection',id:editing.id,name:editing.name,env:editing.env || '',...(editing.provider==='custom'?{base_url:editing.base_url}:{}),...(secret?{api_key:secret}:{})});setSecret('');setEditing(null);await refresh();setMessage('Connection saved.');})}>Save connection</button>
            </fieldset>}
            {login && <div role="status" className="border rounded p-3 my-3"><strong>Provider sign-in: {login.status}</strong>
                {login.events.map((e,i)=><div key={i} className="my-2 text-sm">{e.message}{e.instructions && <p>{e.instructions}</p>}{e.userCode && <p>Code: <strong>{e.userCode}</strong></p>}{(e.url || e.verificationUri) && <a className="text-blue-700 underline" href={e.url || e.verificationUri} target="_blank" rel="noreferrer">Open provider sign-in</a>}{e.links?.map(link=><a key={link.url} className="block text-blue-700 underline" href={link.url} target="_blank" rel="noreferrer">{link.label || 'Provider instructions'}</a>)}</div>)}
                {login.error && <p>{login.error}</p>}
                {login.prompt && <div><label>{login.prompt.message}</label>{login.prompt.type==='select'?<select className="border p-2 w-full" value={reply} onChange={e=>setReply(e.target.value)}><option value="">Choose…</option>{login.prompt.options?.map(o=><option key={o.id} value={o.id}>{o.label}</option>)}</select>:<input className="border p-2 w-full" type={login.prompt.type==='secret'?'password':'text'} autoComplete="off" value={reply} onChange={e=>setReply(e.target.value)}/>}
                    <button disabled={!reply || busy} onClick={()=>void run(async()=>{await api('',{action:'reply',id:login.id,prompt:login.prompt!.id,value:reply});setReply('');})}>Continue sign-in</button></div>}
                {login.status==='pending' && <button onClick={()=>void run(async()=>{await api('',{action:'cancel',id:login.id});setLogin(null);})}>Cancel sign-in</button>}
            </div>}
            <label htmlFor="model-search" className="block text-sm mt-5">Filter loaded models</label><input id="model-search" className="border rounded p-2 w-full mb-3" placeholder="Search model names…" value={search} onChange={e=>setSearch(e.target.value)}/>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">{picker('chat')}{picker('workflow')}</div>
            <div className="flex justify-between items-center gap-3 mt-4"><p className="text-xs text-gray-500">Test model makes one small request using the selected connection.</p><button className="copilot-model-save bg-blue-600 text-white rounded px-4 py-2" disabled={busy || !settings.chat.model || (settings.workflow!==null && !settings.workflow.model)} onClick={()=>void run(async()=>{
                await api('',{action:'defaults',chat:settings.chat,workflow:settings.workflow});
                ['openaiApiKey','openaiBaseUrl','workflowLLMApiKey','workflowLLMBaseUrl','workflowLLMModel','models_list','models_time','models_selected'].forEach(key=>localStorage.removeItem(key));
                onConfigurationUpdated?.();setMessage('Model defaults saved.');
            })}>Save model defaults</button></div>
        </>}
    </Modal>;
}
