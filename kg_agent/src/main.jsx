import React, { useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import {
  ArrowLeft, Bot, Boxes, CheckCircle2, ChevronRight, CircleDot, Database, GitBranch,
  LoaderCircle, MessageSquareText, Network, Play, RefreshCw, Send,
} from 'lucide-react'
import './styles.css'

const api = async (url, options) => {
  const response = await fetch(url, options)
  const data = await response.json()
  if (!response.ok) throw new Error(data.error || `Request failed: ${response.status}`)
  return data
}

const routeFromLocation = () => {
  const path = window.location.pathname
  if (path === '/chat') return { page: 'chat' }
  if (path.startsWith('/repositories/')) {
    return { page: 'repository', name: decodeURIComponent(path.slice('/repositories/'.length)) }
  }
  return { page: 'collection' }
}

const navigate = (path) => {
  window.history.pushState({}, '', path)
  window.dispatchEvent(new PopStateEvent('popstate'))
}

const openNeo4jBrowser = () => {
  const secure = window.location.protocol === 'https:'
  const scheme = secure ? 'bolt+s' : 'bolt'
  const host = secure
    ? `${window.location.hostname}:${window.location.port || '443'}`
    : window.location.host
  const connectURL = encodeURIComponent(`${scheme}://${host}`)
  window.location.assign(`/neo4j/browser/?connectURL=${connectURL}`)
}

function StatusBar({ status }) {
  const progress = status.total ? Math.round((status.completed / status.total) * 100) : 0
  const statusDetail = status.repository || (
    status.message.startsWith('収集に失敗')
      ? '接続状態を確認して再実行してください'
      : '次の収集を開始できます'
  )
  return (
    <div className="status-band">
      <div className={`status-mark ${status.running ? 'running' : ''}`}>
        {status.running ? <LoaderCircle size={18} /> : <CheckCircle2 size={18} />}
      </div>
      <div className="status-copy">
        <strong>{status.message}</strong>
        <span>{statusDetail}</span>
      </div>
      <div className="progress-track" aria-label="収集進捗"><span style={{ width: `${progress}%` }} /></div>
      <span className="progress-value">{status.total ? `${status.completed} / ${status.total}` : '--'}</span>
    </div>
  )
}

function Collection({ status, onCollect }) {
  const [repositories, setRepositories] = useState([])
  const [owner, setOwner] = useState('')
  const [error, setError] = useState('')
  useEffect(() => {
    api('/api/repositories').then((data) => {
      setRepositories(data.repositories || [])
      setOwner(data.owner || '')
      setError('')
    }).catch((reason) => setError(reason.message))
  }, [status.running])

  return (
    <main>
      <header className="page-header">
        <div>
          <p className="kicker">COLLECTION CONTROL</p>
          <h1>ナレッジグラフ収集</h1>
          <p className="lead">{owner || 'Organization 未設定'} の解析状況と対象リポジトリ</p>
        </div>
        <button className="primary" onClick={onCollect} disabled={status.running}>
          {status.running ? <RefreshCw size={18} /> : <Play size={18} />}
          {status.running ? '収集中' : '収集を開始'}
        </button>
      </header>
      <StatusBar status={status} />
      <section className="section-heading">
        <div><p className="kicker">REPOSITORIES</p><h2>対象リポジトリ</h2></div>
        <span className="count">{repositories.length}</span>
      </section>
      {error && <p className="error">{error}</p>}
      <div className="repo-list">
        {repositories.map((repository) => (
          <button className="repo-row" key={repository.full_name} onClick={() => navigate(`/repositories/${encodeURIComponent(repository.full_name)}`)}>
            <span className="repo-icon"><GitBranch size={19} /></span>
            <span className="repo-main"><strong>{repository.name}</strong><small>{repository.description || repository.full_name}</small></span>
            <span className="repo-date">{repository.pushed_at ? new Date(repository.pushed_at).toLocaleDateString('ja-JP') : '--'}</span>
            <ChevronRight size={18} />
          </button>
        ))}
        {!error && repositories.length === 0 && <div className="empty">収集済みリポジトリはありません</div>}
      </div>
    </main>
  )
}

const countLabels = {
  functions: '関数', classes: 'クラス', files: 'ファイル', dependencies: '依存ライブラリ',
  commits: 'コミット', issues: 'Issue', users: '開発者',
}

function Repository({ name }) {
  const [detail, setDetail] = useState(null)
  const [error, setError] = useState('')
  useEffect(() => {
    api(`/api/repositories/${encodeURIComponent(name)}`)
      .then(setDetail)
      .catch((reason) => setError(reason.message))
  }, [name])
  if (error) return <main><button className="back" onClick={() => navigate('/')}><ArrowLeft size={17} />一覧へ</button><p className="error">{error}</p></main>
  if (!detail) return <main className="loading"><LoaderCircle size={28} />読み込み中</main>
  return (
    <main>
      <button className="back" onClick={() => navigate('/')}><ArrowLeft size={17} />一覧へ</button>
      <header className="detail-header">
        <div className="repo-emblem"><Boxes size={30} /></div>
        <div><p className="kicker">REPOSITORY DETAIL</p><h1>{detail.name}</h1><p className="lead">{detail.full_name}</p></div>
      </header>
      <div className="commit-strip">
        <div><span>HEAD COMMIT</span><code>{detail.head_oid || '--'}</code></div>
        <div><span>LAST COMMIT</span><strong>{detail.last_commit_at ? new Date(detail.last_commit_at).toLocaleString('ja-JP') : '--'}</strong></div>
      </div>
      <section className="metrics">
        {Object.entries(countLabels).map(([key, label]) => <div className="metric" key={key}><span>{label}</span><strong>{detail.counts[key] ?? 0}</strong></div>)}
      </section>
      <section className="graph-section">
        <div className="section-heading"><div><p className="kicker">RELATIONSHIPS</p><h2>Call Graph ノード数</h2></div><CircleDot size={24} /></div>
        <div className="call-tree">
          <div className="metric"><span>ノード</span><strong>{detail.counts.functions ?? 0}</strong></div>
        </div>
      </section>
    </main>
  )
}

function Chat() {
  const [input, setInput] = useState('')
  const [messages, setMessages] = useState([{ role: 'agent', text: 'ナレッジグラフについて質問してください。' }])
  const [loading, setLoading] = useState(false)
  const submit = async (event) => {
    event.preventDefault()
    if (!input.trim() || loading) return
    const question = input.trim()
    setInput('')
    setMessages((items) => [...items, { role: 'user', text: question }])
    setLoading(true)
    try {
      const response = await api('/api/chat', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ message: question }) })
      setMessages((items) => [...items, { role: 'agent', text: response.message, sources: response.sources || [] }])
    } catch (reason) {
      setMessages((items) => [...items, { role: 'error', text: reason.message }])
    } finally {
      setLoading(false)
    }
  }
  return (
    <main className="chat-page">
      <header className="page-header"><div><p className="kicker">KNOWLEDGE ASSISTANT</p><h1>AIエージェント</h1><p className="lead">Vertex AI + Neo4j GraphRAG</p></div><Bot size={38} /></header>
      <div className="conversation" aria-live="polite">
        {messages.map((message, index) => (
          <div className={`message ${message.role}`} key={index}>
            <div className="message-text">{message.text}</div>
            {message.sources?.length > 0 && (
              <details className="evidence">
                <summary>Neo4j 根拠 {message.sources.length}件</summary>
                <div className="evidence-list">{message.sources.map((source, sourceIndex) => (
                  <div className="evidence-row" key={`${source.repository}:${source.file}:${source.function}:${sourceIndex}`}>
                    <GitBranch size={15} />
                    <span>
                      <strong>{source.repository}</strong>
                      <code>{source.file}{source.function ? `:${source.line || '?'} · ${source.function} · 距離 ${source.depth}` : ''}</code>
                    </span>
                  </div>
                ))}</div>
              </details>
            )}
          </div>
        ))}
        {loading && <div className="message agent pending"><LoaderCircle size={18} />Neo4j を検索して回答を生成中</div>}
      </div>
      <form className="composer" onSubmit={submit}>
        <input value={input} onChange={(event) => setInput(event.target.value)} placeholder="例: repository-a の process_order を変更した際の影響範囲は？" aria-label="メッセージ" disabled={loading} />
        <button type="submit" title="送信" disabled={loading || !input.trim()}><Send size={19} /></button>
      </form>
    </main>
  )
}

function App() {
  const [route, setRoute] = useState(routeFromLocation)
  const [status, setStatus] = useState({ running: false, message: '接続中', repository: '', completed: 0, total: 0 })
  useEffect(() => {
    const onPopState = () => setRoute(routeFromLocation())
    window.addEventListener('popstate', onPopState)
    return () => window.removeEventListener('popstate', onPopState)
  }, [])
  useEffect(() => {
    const events = new EventSource('/api/status')
    events.onmessage = (event) => setStatus(JSON.parse(event.data))
    return () => events.close()
  }, [])
  const collect = () => api('/api/collect', { method: 'POST' }).catch(() => {})
  return (
    <div className="app-shell">
      <aside>
        <button className="brand" onClick={() => navigate('/')}>
          <img className="brand-logo" src="/ops-frontier-logo-white.svg" alt="Ops Frontier" />
          <img className="brand-mark" src="/favicon.ico" alt="" />
          <span>ナレッジグラフツール</span>
        </button>
        <nav>
          <button className={route.page !== 'chat' ? 'active' : ''} onClick={() => navigate('/')}><Network size={19} />収集管理</button>
          <button className={route.page === 'chat' ? 'active' : ''} onClick={() => navigate('/chat')}><MessageSquareText size={19} />AIチャット</button>
          <button onClick={openNeo4jBrowser}><Database size={19} />Neo4j Browser</button>
        </nav>
        <div className="side-status"><span className={status.running ? 'pulse' : ''} />{status.running ? '収集中' : 'API 接続済み'}</div>
      </aside>
      {route.page === 'chat' ? <Chat /> : route.page === 'repository' ? <Repository name={route.name} /> : <Collection status={status} onCollect={collect} />}
    </div>
  )
}

createRoot(document.getElementById('root')).render(<App />)