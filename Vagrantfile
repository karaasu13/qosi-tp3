# VM do TP3 QoSI: Ubuntu 24.04 com CORE 9.2.1, Docker e Open vSwitch.
#
#   vagrant up          # cria e provisiona (1.ª vez demora ~10 min)
#   vagrant ssh         # entra na VM; o repositório está em /vagrant
#   vagrant ssh -c "cd /vagrant && make teste"
#
# Recursos ajustáveis sem mexer no ficheiro:
#   QOSI_MEM=6144 QOSI_CPUS=4 vagrant up

Vagrant.configure("2") do |config|
  config.vm.box = "bento/ubuntu-24.04"
  config.vm.hostname = "qosi-tp3"
  config.vm.boot_timeout = 600

  # core-gui no ecrã do host: vagrant ssh -- -X core-gui
  config.ssh.forward_x11 = true

  config.vm.provider "virtualbox" do |vb|
    vb.name = "qosi-tp3"
    vb.memory = ENV.fetch("QOSI_MEM", "4096").to_i
    vb.cpus = ENV.fetch("QOSI_CPUS", "4").to_i
  end

  config.vm.provision "shell", path: "provision/provision.sh"
end
