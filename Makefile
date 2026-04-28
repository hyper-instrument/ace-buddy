.PHONY: help install uninstall start stop status flash

PYTHON := python3
PIP := pip

help: ## 显示帮助信息
	@echo "ace-buddy — M5StickC hardware companion for Claude Code"
	@echo ""
	@echo "Targets:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install: ## 安装 ace-buddy (pyserial + hooks + marketplace 插件)
	@echo "==> 安装 Python 依赖 (pyserial)..."
	@$(PYTHON) -m pip install pyserial -q 2>/dev/null || $(PIP) install pyserial -q
	@echo "==> 合并 hooks 到 ~/.claude/settings.json..."
	@bash scripts/install-hooks.sh
	@echo "==> 注册 Claude Code marketplace 插件..."
	@if command -v claude >/dev/null 2>&1; then \
		claude plugin marketplace add "$(shell pwd)" 2>/dev/null && \
		echo "    marketplace 已注册" || echo "    marketplace 注册跳过"; \
		claude plugin install ace-buddy@ace-buddy 2>/dev/null && \
		echo "    插件已安装" || \
		echo "    插件安装跳过 (可手动: claude plugin marketplace add $$(pwd) && claude plugin install ace-buddy@ace-buddy)"; \
	else \
		echo "    Claude Code CLI 未安装, 跳过插件注册"; \
	fi
	@echo ""
	@echo "✅ ace-buddy 安装完成"
	@echo "   在 Claude Code 中使用 /ace-buddy:start 启动 daemon"
	@echo "   或手动: make start"

uninstall: ## 卸载 ace-buddy
	@echo "==> 停止 daemon..."
	@bash scripts/stop.sh 2>/dev/null || true
	@echo "==> 卸载 Claude Code 插件..."
	@-claude plugin uninstall ace-buddy@ace-buddy 2>/dev/null || true
	@echo "==> 清理 marketplace 注册..."
	@$(PYTHON) -c "import json, os; p=os.path.expanduser('~/.claude/settings.json'); d=json.load(open(p)); d.get('enabledPlugins',{}).pop('ace-buddy', None); d.get('extraKnownMarketplaces',{}).pop('ace-buddy', None); json.dump(d, open(p,'w'), indent=2)" 2>/dev/null || true
	@echo "==> 清理状态目录..."
	@rm -rf ~/.ace-buddy
	@echo "✅ ace-buddy 已卸载"
	@echo "   注意: ~/.claude/settings.json 中的 hooks 需手动移除"

start: ## 启动 bridge daemon
	@bash scripts/start.sh

stop: ## 停止 bridge daemon
	@bash scripts/stop.sh

status: ## 查看 daemon 状态
	@bash scripts/status.sh

flash: ## 编译并刷写固件 (需要 PlatformIO + USB 连接设备)
	@bash scripts/stop.sh 2>/dev/null || true
	@bash scripts/flash.sh
	@bash scripts/start.sh
