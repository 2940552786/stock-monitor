## 项目结构
```
C:\Users\29405\分时图\
├── server.py           # Flask 后端代理
├── index.html          # 前端页面
└── requirements.txt    # Python 依赖
```

## 实现步骤

### Step 1: 创建 Flask 后端 (server.py)
- 代理新浪/东方财富 API 解决跨域
- 路由: /api/quote (实时报价), /api/trend (分时数据), /api/chip (筹码分布)
- 简单内存缓存避免频繁请求

### Step 2: 创建前端页面 (index.html)
- ECharts 分时图 (价格线+均价线+成交量)
- 顶底检测算法 + 图表标记
- 筹码分布图 (横向山峰图)
- 自选股管理 (localStorage)
- 10秒自动刷新 + 倒计时

### Step 3: 创建 requirements.txt