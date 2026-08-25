# ── Stage 1: 构建 ──
FROM node:20-alpine AS builder
WORKDIR /app
COPY package.json yarn.lock* ./
RUN yarn install --frozen-lockfile --production=false
COPY . .
RUN yarn build

# ── Stage 2: 运行 ──
FROM node:20-alpine
WORKDIR /app
ENV NODE_ENV=production

# 只复制运行时所需文件
COPY --from=builder /app/.next ./.next
COPY --from=builder /app/node_modules ./node_modules
COPY --from=builder /app/package.json ./package.json
COPY --from=builder /app/public ./public

EXPOSE 3000
CMD ["yarn", "start"]
