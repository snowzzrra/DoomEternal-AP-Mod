FROM node:24.19.0-alpine
WORKDIR /service
COPY server.cjs start.sh ./
RUN mkdir /data && chown node:node /data
EXPOSE 8080
CMD ["sh", "start.sh"]
