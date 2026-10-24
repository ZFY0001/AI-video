import { createRoot } from 'react-dom/client'

import App from './App'
import './index.css'

// 不套 StrictMode：开发态它会双跑 effect，导致同一个任务被订阅两次、日志重复。
const container = document.getElementById('root')
if (!container) throw new Error('缺少 #root 挂载点')

createRoot(container).render(<App />)
