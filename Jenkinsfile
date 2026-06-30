// CI/CD for the counters stack.
//
// Jenkins polls `main` every minute (pollSCM below); on a new commit it checks
// out the repo, rebuilds the app image, and redeploys the docker-compose stack
// on the host (through the mounted docker.sock). Polling needs no public
// exposure, so it works behind NAT; swap in a GitHub webhook later by adding
// the "GitHub hook trigger" once jenkins.bitcoincounters.com is reachable.
//
// Prerequisites in Jenkins:
//   * A "Secret file" credential with ID `counters-env` containing the real
//     production .env (RPC creds, etc.). The repo only ships .env.example.
//   * The controller image includes the Docker CLI + compose plugin and the
//     host docker.sock is mounted (see ../jenkins/docker-compose.yml).

pipeline {
  agent any

  options {
    timestamps()
    disableConcurrentBuilds()
    buildDiscarder(logRotator(numToKeepStr: '20'))
  }

  triggers {
    // Check GitHub for new commits to the configured branch every minute.
    pollSCM('* * * * *')
  }

  environment {
    // Pin the compose project so we UPDATE the existing `counters` stack
    // instead of spinning up a parallel one (which would clash on port 8081).
    COMPOSE_PROJECT_NAME = 'counters'
  }

  stages {
    stage('Checkout') {
      steps {
        checkout scm
        sh 'git rev-parse --short HEAD'
      }
    }

    stage('Provision .env') {
      steps {
        withCredentials([file(credentialsId: 'counters-env', variable: 'ENV_FILE')]) {
          sh 'install -m 600 "$ENV_FILE" .env'
        }
      }
    }

    stage('Build image') {
      steps {
        sh 'docker compose build'
      }
    }

    stage('Deploy') {
      steps {
        sh 'docker compose up -d --remove-orphans'
      }
    }

    stage('Smoke test') {
      steps {
        // Give the server a moment, then verify the explorer responds.
        sh '''
          for i in $(seq 1 15); do
            if curl -fsS -o /dev/null http://host.docker.internal:8081/status; then
              echo "explorer is up"; exit 0
            fi
            sleep 2
          done
          echo "explorer did not become healthy" >&2
          docker compose logs --tail=50 counters >&2 || true
          exit 1
        '''
      }
    }
  }

  post {
    always {
      sh 'docker compose ps || true'
      sh 'rm -f .env || true'
    }
    failure {
      echo 'Deploy failed — the previous containers keep running (unless-stopped).'
    }
  }
}
