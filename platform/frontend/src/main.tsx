import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
// 顺序很重要：React Flow 的默认样式必须先加载，自己的主题放后面才能覆盖它。
// 反过来的话，.react-flow__node-default 和 .dag-node 优先级相同，
// 后加载的那份赢 —— 结果就是自己写的节点配色全不生效。
import '@xyflow/react/dist/style.css'
import './styles.css'
import App from './App'

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </React.StrictMode>,
)
