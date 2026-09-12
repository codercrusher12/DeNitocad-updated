FROM node:22-slim

WORKDIR /app

COPY package.json .
RUN npm install --omit=dev

COPY . .

ENV PORT=8403
EXPOSE 8403

CMD ["npm", "start"]
