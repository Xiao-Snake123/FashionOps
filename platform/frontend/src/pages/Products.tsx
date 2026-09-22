import { useEffect, useState } from 'react'
import { api, type Product } from '../api'
import { PageHeader, StatusBadge, yuan } from '../ui'

export default function Products() {
  const [items, setItems] = useState<Product[]>([])
  const [total, setTotal] = useState(0)
  const [categories, setCategories] = useState<{ value: string; count: number }[]>([])
  const [seasons, setSeasons] = useState<{ value: string; count: number }[]>([])
  const [category, setCategory] = useState('')
  const [season, setSeason] = useState('')
  const [keyword, setKeyword] = useState('')
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api
      .productFacets()
      .then((f) => {
        setCategories(f.categories)
        setSeasons(f.seasons)
      })
      .catch(() => undefined)
  }, [])

  useEffect(() => {
    const timer = setTimeout(() => {
      api
        .products({
          page_size: 50,
          category: category || undefined,
          season: season || undefined,
          q: keyword || undefined,
        })
        .then((page) => {
          setItems(page.items)
          setTotal(page.total)
          setError(null)
        })
        .catch((e: Error) => setError(e.message))
    }, 220) // 输入防抖：别每敲一个字就打一次接口
    return () => clearTimeout(timer)
  }, [category, season, keyword])

  return (
    <>
      <PageHeader
        crumbs={['运营中台', '商品库']}
        title="商品库"
        badge={<span className="tag">共 {total} 个 SPU</span>}
        desc="选品 Agent 会按类目 + 季节 + 风格标签找相似竞品，再叠加库存硬约束过滤。"
      />

      {error && <div className="error-box">{error}</div>}

      <div className="toolbar">
        <input
          type="text"
          placeholder="搜索标题…"
          value={keyword}
          onChange={(e) => setKeyword(e.target.value)}
          style={{ width: 190 }}
        />
        <select value={category} onChange={(e) => setCategory(e.target.value)}>
          <option value="">全部类目</option>
          {categories.map((c) => (
            <option key={c.value} value={c.value}>
              {c.value}（{c.count}）
            </option>
          ))}
        </select>
        <select value={season} onChange={(e) => setSeason(e.target.value)}>
          <option value="">全部季节</option>
          {seasons.map((s) => (
            <option key={s.value} value={s.value}>
              {s.value}（{s.count}）
            </option>
          ))}
        </select>
      </div>

      <div className="card">
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>SPU</th>
                <th>标题</th>
                <th>类目</th>
                <th>季节</th>
                <th>风格标签</th>
                <th>成本</th>
                <th>吊牌价</th>
                <th>毛利率</th>
                <th>评分</th>
                <th>状态</th>
              </tr>
            </thead>
            <tbody>
              {items.map((product) => {
                const cost = Number(product.cost_price ?? 0)
                const price = Number(product.list_price ?? 0)
                const margin = price > 0 ? (1 - cost / price) * 100 : null
                return (
                  <tr key={product.id}>
                    <td className="mono">{product.spu_code}</td>
                    <td>{product.title}</td>
                    <td className="muted">
                      {product.category}
                      {product.sub_category ? ` / ${product.sub_category}` : ''}
                    </td>
                    <td>{product.season ?? '—'}</td>
                    <td className="muted">{(product.style_tags ?? []).join(' · ') || '—'}</td>
                    <td>{yuan(product.cost_price)}</td>
                    <td>{yuan(product.list_price)}</td>
                    <td>
                      {margin === null ? (
                        '—'
                      ) : (
                        <span className={`badge ${margin < 55 ? 'warn' : 'ok'}`}>{margin.toFixed(1)}%</span>
                      )}
                    </td>
                    <td>{product.rating ?? '—'}</td>
                    <td>
                      <StatusBadge status={product.status} />
                    </td>
                  </tr>
                )
              })}
              {items.length === 0 && (
                <tr>
                  <td colSpan={10} className="empty">
                    没有匹配的商品
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </>
  )
}
