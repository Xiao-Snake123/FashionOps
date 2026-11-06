import ReactMarkdown, { type Components } from 'react-markdown'
import remarkBreaks from 'remark-breaks'
import remarkGfm from 'remark-gfm'

/**
 * 助手正文的 Markdown 渲染。
 *
 * ## 为什么用 react-markdown，而不是自己写一个
 *
 * 模型输出天然是 Markdown（`**加粗**`、编号列表、表格、代码块）。自己写解析器
 * 有两个过不去的坎：
 *
 * 1. **正确性**：行内格式要能嵌进块级结构里 —— 比如列表项里带加粗
 *    （`1. **查询数据**：……`，这正是最常见的形态），还有"代码块里的 `*`
 *    不能被当成斜体"、"链接文字里的方括号怎么嵌套"。这些边界写不完，
 *    而渲染错的表现是**正文缺字或错位**，比不渲染更糟。
 * 2. **安全**：自己写迟早会走到拼 HTML 那条路（`dangerouslySetInnerHTML`），
 *    而正文是模型生成的、内容不完全可控。
 *
 * react-markdown 把 Markdown 解析成 **React 元素树**输出，全程不碰 innerHTML，
 * 结构上就没有注入的口子。它默认**也不渲染原始 HTML** —— 注意别为了"顺便支持
 * HTML"去加 `rehype-raw`，那等于把上面这层保护拆掉。
 *
 * ## 三个刻意的选择
 *
 * * `remarkGfm`：表格 / 删除线 / 任务列表 / 自动把裸链接变成可点链接。
 *   这个平台的回答里会出现报表类表格，没有它表格会退化成一堆竖线。
 * * `remarkBreaks`：把**单个换行**当换行。CommonMark 规定"段内单换行 = 空格"，
 *   但聊天里模型的换行是它有意排的版，合并成空格会把内容糊成一整段。
 * * 流式光标**不在这里放 span**，而是用 CSS `::after` 挂在最后一个块级元素上
 *   （见 `chat.css` 的 `.md-streaming`）。Markdown 渲染出来是块级的，
 *   JSX 里写一个 `<span className="caret" />` 会掉到最后一个 `<p>`
 *   **下面**单独占一行，看着像光标掉下去了。
 */

/**
 * 插件数组提到模块作用域是**有意的**：传给 ReactMarkdown 的必须是稳定引用，
 * 否则每次渲染都会重建一遍解析流水线（流式时每来一个 token 就重建一次）。
 */
const REMARK_PLUGINS = [remarkGfm, remarkBreaks]

/**
 * 自定义元素。
 *
 * 这里刻意逐个**显式取用属性**，而不是 `({ node, ...rest })` 那样透传：
 * react-markdown 会塞一个 `node`（语法树节点）进来，它不是合法 DOM 属性，
 * 展开到元素上会让 React 在控制台刷 "does not recognize the `node` prop"。
 */
const COMPONENTS: Components = {
  a: ({ href, title, children }) => (
    // 正文里的链接是模型给的，留在本页点开会把用户的对话整个覆盖掉，所以一律新窗口。
    <a href={href} title={title} target="_blank" rel="noreferrer noopener">
      {children}
    </a>
  ),
  table: ({ children }) => (
    // 报表类表格列多，不套一层横向滚动容器会把气泡撑破、连带把整个聊天区挤歪。
    <div className="md-table-wrap">
      <table>{children}</table>
    </div>
  ),
}

export function Markdown({ text, streaming = false }: { text: string; streaming?: boolean }) {
  return (
    <div className={`md${streaming ? ' md-streaming' : ''}`}>
      <ReactMarkdown remarkPlugins={REMARK_PLUGINS} components={COMPONENTS}>
        {text}
      </ReactMarkdown>
    </div>
  )
}
