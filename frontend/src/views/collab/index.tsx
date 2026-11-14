import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/collab'
const columns = [
  "看板编号",
  "看板名称",
  "负责人",
  "协作成员",
  "图表集合",
  "刷新频率",
  "评论开关",
  "当前版本",
  "创建时间",
  "看板状态"
]
const actions = [
  "发布看板",
  "归档看板",
  "删除看板"
]
const statuses = [
  "草稿",
  "协作中",
  "已发布",
  "已归档"
]
const stats = [
  {
    "label": "协作看板总数",
    "value": 0
  },
  {
    "label": "协作中看板",
    "value": 0
  },
  {
    "label": "已发布看板",
    "value": 0
  }
]

type Row = Record<string, string | number | null>
type VersionRow = Record<string, unknown> & { 版本号: number; 版本状态: string }

async function readError(response: Response, fallback: string): Promise<string> {
  try {
    const payload = await response.json()
    const detail = payload?.detail
    if (typeof detail === 'string') return detail
    if (detail && typeof detail === 'object' && 'message' in detail) return String(detail.message)
    return payload?.message ?? fallback
  } catch {
    return fallback
  }
}

export default function CollabView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [infoMessage, setInfoMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [operator, setOperator] = useState('')
  const [versions, setVersions] = useState<VersionRow[]>([])
  const [versionBoard, setVersionBoard] = useState('')
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('协作看板列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '看板协作管理列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  function requireOperator(): string {
    const name = operator.trim()
    if (!name) {
      throw new Error('请先在页面上方填写操作人，导出入口与变更操作都要做权限与归属校验')
    }
    return name
  }

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setInfoMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action } }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message ?? payload.detail ?? '看板协作管理动作未生效，请稍后重试')
      }
      setInfoMessage(payload.message ?? '操作已完成')
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '看板协作管理操作失败')
    }
  }

  async function adjustFrequency(row: Row) {
    setErrorMessage('')
    setInfoMessage('')
    try {
      const name = requireOperator()
      const frequency = window.prompt('新的刷新频率', String(row['刷新频率'] ?? ''))
      if (frequency === null || !frequency.trim()) return
      const members = window.prompt('协作成员（顿号或逗号分隔，留空表示沿用当前成员范围）', '')
      if (members === null) return
      const note = window.prompt('变更说明（可留空）', '') ?? ''
      const values: Record<string, unknown> = {
        operator: name,
        刷新频率: frequency.trim(),
        base_version: row['当前版本'],
        变更说明: note,
      }
      if (members.trim()) values['协作成员'] = members.trim()
      const response = await request(`${ENDPOINT}/${row.id}/refresh-frequency`, {
        method: 'POST',
        body: JSON.stringify({ values }),
      })
      if (!response.ok) {
        throw new Error(await readError(response, '刷新频率调整未生效，请稍后重试'))
      }
      const payload = await response.json()
      setInfoMessage(payload.message ?? '刷新频率已调整')
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '刷新频率调整失败')
    }
  }

  async function refreshResult(row: Row) {
    setErrorMessage('')
    setInfoMessage('')
    try {
      const name = requireOperator()
      const succeed = window.confirm('本次刷新是否成功？「确定」按成功发布，「取消」按失败记录')
      let reason = ''
      if (!succeed) {
        reason = window.prompt('失败原因', '数据源连接超时') ?? ''
      }
      const response = await request(`${ENDPOINT}/${row.id}/refresh-result`, {
        method: 'POST',
        body: JSON.stringify({ values: { operator: name, 成功: succeed, 原因: reason } }),
      })
      if (!response.ok) {
        throw new Error(await readError(response, '发布结果刷新未生效，请稍后重试'))
      }
      const payload = await response.json()
      setInfoMessage(payload.message ?? '发布结果已刷新')
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '发布结果刷新失败')
    }
  }

  async function createShareLink(row: Row) {
    setErrorMessage('')
    setInfoMessage('')
    try {
      const name = requireOperator()
      const response = await request(`${ENDPOINT}/${row.id}/share-links`, {
        method: 'POST',
        body: JSON.stringify({ values: { operator: name } }),
      })
      if (!response.ok) {
        throw new Error(await readError(response, '共享链接签发失败'))
      }
      const payload = await response.json()
      const token = payload?.data?.link?.令牌 ?? ''
      setInfoMessage(`${payload.message}：/api/collab/shared/${token}（成员范围收紧后旧链接会受控失效）`)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '共享链接签发失败')
    }
  }

  async function loadVersions(row: Row) {
    setErrorMessage('')
    setInfoMessage('')
    try {
      const name = requireOperator()
      const query = new URLSearchParams({ operator: name }).toString()
      const response = await request(`${ENDPOINT}/${row.id}/versions?${query}`)
      if (!response.ok) {
        throw new Error(await readError(response, '版本记录读取失败'))
      }
      const payload = await response.json()
      setVersions(payload.items ?? [])
      setVersionBoard(String(row['看板编号'] ?? ''))
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '版本记录读取失败')
    }
  }

  function exportBoard(row: Row) {
    setErrorMessage('')
    try {
      const name = requireOperator()
      const query = new URLSearchParams({ operator: name }).toString()
      window.open(`${ENDPOINT}/${row.id}/export?${query}`, '_blank')
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '导出失败')
    }
  }

  return (
    <section className="page" data-module="collab">
      <header className="page-head">
        <div>
          <h2>看板协作管理管理</h2>
          <p className="page-desc">维护协作看板的成员配置、图表组合与协作功能，为看板协作管理提供可验证的数据工程流程。</p>
        </div>
        <div className="page-actions">
          <label className="filter-item">
            <span>操作人</span>
            <input
              value={operator}
              placeholder="如：王岚 / admin / 协作成员"
              onChange={(event) => setOperator(event.target.value)}
            />
          </label>
          <button className="btn primary" type="button" onClick={() => setErrorMessage('协作看板登记入口尚未接入审批流')}>
            登记协作看板
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出看板协作管理清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
                <button className="link" type="button" onClick={() => void adjustFrequency(row)}>调整刷新频率</button>
                <button className="link" type="button" onClick={() => void refreshResult(row)}>刷新发布结果</button>
                <button className="link" type="button" onClick={() => void createShareLink(row)}>共享链接</button>
                <button className="link" type="button" onClick={() => void loadVersions(row)}>版本记录</button>
                <button className="link" type="button" onClick={() => exportBoard(row)}>导出</button>
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无看板协作管理数据，可先登记协作看板</td>
            </tr>
          )}
        </tbody>
      </table>

      {versions.length > 0 && (
        <section className="version-panel">
          <h3>{versionBoard} 版本记录（旧版本按当时口径保留）</h3>
          <table className="data-table">
            <thead>
              <tr>
                <th>版本号</th>
                <th>版本状态</th>
                <th>刷新频率</th>
                <th>成员范围</th>
                <th>发布结果</th>
                <th>共享链接</th>
                <th>变更说明</th>
                <th>创建时间</th>
              </tr>
            </thead>
            <tbody>
              {versions.map((version) => {
                const publish = (version['发布结果'] ?? {}) as Record<string, unknown>
                const links = (version['共享链接'] ?? []) as Array<Record<string, unknown>>
                return (
                  <tr key={String(version['版本号'])}>
                    <td>v{version['版本号']}</td>
                    <td>{String(version['版本状态'] ?? '—')}</td>
                    <td>{String(version['刷新频率'] ?? '—')}</td>
                    <td>{((version['成员范围'] ?? []) as string[]).join('、') || '—'}</td>
                    <td>{publish['发布状态'] ? `批次${publish['批次']} · ${publish['发布状态']}` : '—'}</td>
                    <td>{links.length ? links.map((link) => `${link['状态']}`).join('、') : '—'}</td>
                    <td>{String(version['变更说明'] ?? '—')}</td>
                    <td>{String(version['创建时间'] ?? '—')}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </section>
      )}

      <footer className="page-foot">
        <span>共 {total} 条看板协作管理记录</span>
        {infoMessage && <span className="info-text">{infoMessage}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
