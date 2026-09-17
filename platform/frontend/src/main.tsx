import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
// 样式表顺序有讲究：通用主题先加载，后面各页的专用规则才能覆盖它。
// （原来这里第一份是 React Flow 的样式 —— 任务详情页的 DAG 图已经换成
// todo 列表，那个依赖和它的样式一起不用了，但"顺序即优先级"这条不变。）
import './styles.css'
// 认证相关样式放最后：它里面有 .login-card .button 这类会覆盖通用按钮样式的规则，
// 必须在 styles.css 之后加载才生效
import './auth.css'
// 对话页的样式。它里面有 .tool-chip / .bubble 这类只在对话页用的规则，
// 放最后避免被前面的通用规则盖掉
import './chat.css'
// 客服工单页的样式（判定来源标签、草稿框、选中行）。同样放最后
import './service.css'
import App from './App'

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </React.StrictMode>,
)
