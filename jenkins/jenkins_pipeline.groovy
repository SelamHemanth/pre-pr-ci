//-----------------------------------------------------------------------------
//Groovy Script for run the jenkins pipeline.
//Author: Priyanka Mani
//Email: DMani.Priyanka@amd.com
//-----------------------------------------------------------------------------

// ===================== MAIN PIPELINE ============================

pipeline {
    agent { label "${params.node_name}" }

    parameters {
        choice(
            name: 'node_name',
            choices: ['none', '99fc_genoa', '9b0c_turin', '9b5e_genoa', '9dee_turin', '9f6e_genoa', '9f8e_turin', 'titanite_genoa'],
            description: 'Select the node, to run the entire process in particular system'    
        )
        
        choice(
            name: 'Distro',
            choices: ['none', 'anolis', 'openeuler'],
            description: 'Select the distro you want to test'
        )
        
        string(
            name: 'PATCH_DIR',
            defaultValue: 'none',
            trim: true,
            description: 'Linux source code path'
        )
        
        string(
            name: 'SIGNED_OFF_NAME',
            defaultValue: 'mohanasv',
            trim: true,
            description: 'Signed-off-by name'
        )
        
        string(
            name: 'SIGNED_OFF_EMAIL',
            defaultValue: 'mohanasv@amd.com',
            trim: true,
            description: 'Signed-off-by email'
        )
        
        string(
            name: 'BUGZILLA_ID',
            defaultValue: 'none',
            trim: true,
            description: 'Anolis Bugzilla ID'
        )
        
        string(
            name: 'NO_OF_PATCHES',
            defaultValue: 'none',
            trim: true,
            description: 'Number of patches to apply'
        )
		
		choice(
            name: 'Do_Build',
            choices: ['yes', 'no'],
            description: 'Do you want to build the patches'
        )
        
        string(
            name: 'BUILD_THREADS',
            defaultValue: 'none',
            trim: true,
            description: 'Enter the number of CPUs'
        )
        
        string(
            name: 'Host_configuration',
            defaultValue: 'none',
            trim: true,
            description: 'Enter Host user password. If your password contains a $ symbol, escape it with a backslash. Example: Dma\\$1234'
        )
        
        string(
            name: 'VM_ip',
            defaultValue: 'none',
            trim: true,
            description: 'Enter VM IP'
        )
        
        string(
            name: 'VM_root_pwd',
            defaultValue: 'none',
            trim: true,
            description: 'Enter VM root password. If your password contains a $ symbol, escape it with a backslash. Example: "Dma\\$1234"'
        )
        
        choice(
            name: 'Anolis_Selected_tests',
            choices: ['none', 'check_dependency', 'check_Kconfig', 'build_allyes_config', 'build_allno_config', 'build_anolis_defconfig', 'build_anolis_debug', 'anck_rpm_build', 'check_kapi', 'boot_kernel_rpm', 'all'],
            description: 'If your selected distro is anolis, select the test cases'
        )
        
        choice(
            name: 'Patch_category',
            choices: ['none', 'feature', 'bugfix', 'performance', 'security'],
            description: 'If your selected distro is euler, Select one patch category'
        )
        
        choice(
            name: 'Euler_Selected_tests',
            choices: ['none', 'check_dependency', 'build_allmod', 'check_patch', 'check_format', 'rpm_build', 'boot_kernel', 'all'],
            description: 'If your selected distro is euler, Select the test cases'
        )
    }

stages {

        stage('Initialization') {
            steps {
                script {
                    try {
                        echo "=========== DISTRO INITIALIZATION START ==========="                    
                        echo "Workspace: ${env.WORKSPACE}"
                        
                        def distros = detect_system_distro()
                        validate_distro_selection(distros)
                        run_distro_specific_operations(distros.system, distros)
                        
                        echo "=========== DISTRO INITIALIZATION COMPLETE =========="
                    } catch (Exception e) {
                        error("❌ Initialization stage failed: ${e.message}")
                    }
                }
            }
        }
        
        stage('Configuration') {
            steps {
                script {
                    try {
                        echo "=========== CONFIGURATION START ============="
                        
                        def distros = detect_system_distro()
                        echo "Configuring for distro: ${distros.system}"
                        
                        if (distros.system == 'anolis') {
                            anolis_general_configuration()
                        } else if (distros.system == 'openeuler') {
                            euler_general_configuration()
                        } else {
                            error("❌ Unsupported distro: ${distros.system}")
                        }
                        
                        echo "=========== CONFIGURATION COMPLETE ============="
                    } catch (Exception e) {
                        error("❌ Configuration stage failed: ${e.message}")
                    }
                }
            }
        }
        
        stage('Build') {
            steps {
                script {
                    try {
                        echo "============ BUILD START ============="
                        
                        def buildDir = "${env.WORKSPACE}/patch-precheck-ci"
                        
                        if (!fileExists(buildDir)) {
                            error("❌ Build directory not found: ${buildDir}")
                        }
                        
						if(params.Do_Build == 'yes')
						{
							sh """
								cd "${buildDir}"
								make build
							"""
						}
						else{
							echo "Build is not selected, Skipping build"
						}
                        
                        echo "=========== BUILD COMPLETE ============="
                    } catch (Exception e) {
                        error("❌ Build stage failed: ${e.message}")
                    }
                }
            }
        }
        
        stage('Test') {
            steps {
                script {
                    try {
                        echo "============ TESTING START ============="
                        
                        def distros = detect_system_distro()
                        
                        if (distros.system == 'anolis') {
                            anolis_test_configuration()
                        } else if (distros.system == 'openeuler') {
                            euler_test_configuration()
                        } else {
                            error("❌ Unsupported distro for testing: ${distros.system}")
                        }
                        
                        echo "============ TEST COMPLETE ==============="
                    } catch (Exception e) {
                        error("❌ Test stage failed: ${e.message}")
                    }
                }
            }
        }
        
        stage('Archive results') {
            steps {
                script {
                    try {
                        echo '============ ARCHIVING RESULTS ============'
                        MyArchive()
                        echo '============ ARCHIVING COMPLETE ==========='
                    } catch (Exception e) {
                        error("❌ Archive stage failed: ${e.message}")
                    }
                }
            }
        }
    }
    
}

// ===================== FUNCTION DEFINITIONS ======================

def detect_system_distro() {
    try {
        def system = sh(
            script: "awk -F= '/^ID=/{print \$2}' /etc/os-release | tr -d '\"'",
            returnStdout: true
        ).trim().toLowerCase()

        def user = params.Distro.toLowerCase()

        echo "Detected system distro: ${system}"
        echo "User selected distro: ${user}"

        if (!system) {
            error("❌ Failed to detect system distribution")
        }

        return [system: system, user: user]
    } catch (Exception e) {
        error("❌ Error detecting system distro: ${e.message}")
    }
}

def validate_distro_selection(d) {
    try {
        if (d.user == 'none') {
            echo "⚠ No distro selected → Skipping validation."
            return
        }

        if (d.user != d.system) {
            error("❌ MISMATCH — User selected '${d.user}', but system is '${d.system}'")
        }

        echo "✔ MATCH — User selected distro matches system distro."
    } catch (Exception e) {
        error("❌ Distro validation failed: ${e.message}")
    }
}

def run_distro_specific_operations(system_distro, d) {
    try {
        echo "Running distro-specific operations for: ${system_distro}"
        
        switch(system_distro) {
            case 'anolis':
                echo "→ Installing packages for OpenAnolis"
                sh """
                    set -e
                    yum install -y git make automake python3-rpm bc flex bison rpm lz4 libunwind-devel python3-devel slang-devel numactl-devel libbabeltrace-devel capstone-devel libpfm-devel libtraceevent-devel
                    yum install -y audit-libs-devel binutils-devel libbpf-devel libcap-ng-devel libnl3-devel newt-devel pciutils-devel xmlto yum-utils
                    yum install -y openssh-server
                """
                echo "✔ Anolis package installation completed"
                break

            case 'openeuler':
            case 'euler':
                echo "→ Installing packages for OpenEuler"
                sh """
                    set -e
                    dnf install -y git make python3 sshpass
                """
                echo "✔ Euler package installation completed"
                break
                
            default:
                error("❌ Unknown distro: ${system_distro}")
        }
        
        // Clone repository if not exists
        clone_repository()

	//Clone torvalds repository
	clone_torvalds_repo()
        
        // Create distro configuration file
        create_distro_config(d.system)
        
    } catch (Exception e) {
        error("❌ Distro-specific operations failed: ${e.message}")
    }
}

def clone_repository() {
    try {
        def repoDir = "${env.WORKSPACE}/patch-precheck-ci"
        
        if (fileExists(repoDir)) {
            echo "✔ Directory 'patch-precheck-ci' already exists - skipping clone."
            
            // Verify it's a valid git repository
            def isGitRepo = sh(
                script: "cd ${repoDir} && git rev-parse --git-dir > /dev/null 2>&1 && echo 'true' || echo 'false'",
                returnStdout: true
            ).trim()
            
            if (isGitRepo == 'false') {
                error("❌ Directory exists but is not a valid git repository")
            }

	     // Valid git repository exists - Update it
            echo "→ Updating existing repository..."

            sh """
                set -e
                cd ${repoDir}
                git fetch --all
                git pull origin master
            """

            echo "✔ Repository updated successfully"

        } else {
            echo "Directory not found - cloning repository..."
            sh """
                set -e
                cd "${env.WORKSPACE}"
                git clone https://github.com/SelamHemanth/patch-precheck-ci.git
            """
            
            // Verify clone was successful
            if (!fileExists(repoDir)) {
                error("❌ Failed to clone repository")
            }
            
            echo "✔ Repository cloned successfully"
        }
    } catch (Exception e) {
        error("❌ Repository clone failed: ${e.message}")
    }
}

def clone_torvalds_repo() {
    try {
        def torvaldsDir = "${env.WORKSPACE}/patch-precheck-ci/.torvalds-linux"

        echo "=========== TORVALDS REPO SETUP START ==========="

	// function to re-clone repository (with removal)
        def recloneRepo = {
            echo "→ Removing existing directory and re-cloning..."
            sh """
                set -e
                rm -rf ${torvaldsDir}
                cd ${env.WORKSPACE}/patch-precheck-ci
                git clone --bare https://github.com/torvalds/linux.git .torvalds-linux
                git config --global --add safe.directory ${torvaldsDir}
            """
        }

	// function to clone fresh (no removal)
        def cloneFresh = {
            echo "→ Cloning linux repository from torvalds into '.torvalds-linux'..."
            sh """
                set -e
                cd ${env.WORKSPACE}/patch-precheck-ci
                git clone --bare https://github.com/torvalds/linux.git .torvalds-linux
                git config --global --add safe.directory ${torvaldsDir}
            """
        }

        if (!fileExists(torvaldsDir)) {
            // Directory does NOT exist - Clone fresh (no removal needed)
            echo "⚠ Directory '.torvalds-linux' not found"
            cloneFresh()
            echo "✔ Linux repository cloned successfully into '.torvalds-linux'"
        } else {
            // Directory EXISTS - Check if it's a valid git repository
            echo "✔ Directory '.torvalds-linux' already exists: ${torvaldsDir}"
            
            def isValidRepo = sh(
                script: "cd ${torvaldsDir} && git rev-parse --git-dir > /dev/null 2>&1 && echo 'true' || echo 'false'",
                returnStdout: true
            ).trim()
            
            if (isValidRepo == 'false') {
                // Directory exists but NOT a valid git repository
                echo "⚠ Directory exists but is not a valid git repository"
                recloneRepo()
                echo "✔ Linux repository re-cloned successfully"
            } else {
                // Valid git repository exists - Try to update it
                echo "→ Updating existing linux repository..."
                def fetchResult = sh(
                    script: """
                        set -e
                        cd ${torvaldsDir}
                        git fetch --all --tags
                    """,
                    returnStatus: true
                )
                
                if (fetchResult != 0) {
                    // Fetch failed - Re-clone the repository
                    echo "⚠ Failed to fetch updates (exit code: ${fetchResult})"
                    echo "→ Repository may be corrupted. Removing and re-cloning..."
                    recloneRepo()
                    echo "✔ Linux repository re-cloned successfully after fetch failure"
                } else {
                    echo "✔ Linux repository updated successfully"
                }
            }
        }
        
        // Final verification
        if (!fileExists(torvaldsDir)) {
            error("❌ Failed to setup linux repository")
        }
        
        echo "=========== TORVALDS REPO SETUP COMPLETE ==========="
	
    } catch (Exception e) {
        error("❌ Torvalds repo setup failed: ${e.message}")
    }
}

def create_distro_config(distro) {
    try {
        def configFile = "${env.WORKSPACE}/patch-precheck-ci/.distro_config"
        def distroDir = (distro == 'openeuler') ? 'euler' : distro
        def distroName = (distro == 'openeuler') ? 'euler' : distro
        
        echo "Creating .distro_config for: ${distroName}"
        
        // Check if file exists
        if (fileExists(configFile)) {
            echo "⚠ Configuration file already exists - will be overwritten"
        }
        
        // Create configuration file
        sh """
            cd "${env.WORKSPACE}/patch-precheck-ci"
            cat > .distro_config <<EOF
DISTRO=${distroName}
DISTRO_DIR=${distroDir}
EOF
        """
        
        // Verify file was created
        if (!fileExists(configFile)) {
            error("❌ Failed to create .distro_config file")
        }
        
        // Verify file has content
        def fileContent = sh(
            script: "cat ${configFile}",
            returnStdout: true
        ).trim()
        
        if (!fileContent) {
            error("❌ .distro_config file is empty")
        }
        
        if (!fileContent.contains("DISTRO=${distroName}")) {
            error("❌ .distro_config file does not contain expected content")
        }
        
        echo "✔ .distro_config created and validated successfully"
        echo "File content:"
        sh "cat ${configFile}"
        
    } catch (Exception e) {
        error("❌ Failed to create distro config: ${e.message}")
    }
}

def anolis_general_configuration() {
    try {
        def configFile = "${env.WORKSPACE}/patch-precheck-ci/anolis/.configure"
        
        echo "Creating Anolis configuration file..."
        
        // Validate required parameters
        validate_required_params([
            'PATCH_DIR': params.PATCH_DIR,
            'BUILD_THREADS': params.BUILD_THREADS
        ])
        
        // Check if directory exists
        def configDir = "${env.WORKSPACE}/patch-precheck-ci/anolis"
        if (!fileExists(configDir)) {
            error("❌ Anolis directory not found: ${configDir}")
        }
        
        // Check if file exists
        if (fileExists(configFile)) {
            echo "⚠ Configuration file already exists - will be overwritten"
        }
        
        // Create configuration file
        sh """
            cd "${configDir}"
            cat > .configure <<'EOF'
# General Configuration
LINUX_SRC_PATH="${params.PATCH_DIR}"
SIGNER_NAME="${params.SIGNED_OFF_NAME}"
SIGNER_EMAIL="${params.SIGNED_OFF_EMAIL}"
ANBZ_ID="${params.BUGZILLA_ID != 'none' ? params.BUGZILLA_ID.toInteger() : 0}"
NUM_PATCHES="${params.NO_OF_PATCHES != 'none' ? params.NO_OF_PATCHES.toInteger() : 0}"

# Build Configuration
BUILD_THREADS="${params.BUILD_THREADS != 'none' ? params.BUILD_THREADS.toInteger() : 1}"

# Test Configuration    
RUN_TESTS="yes"
TEST_CHECK_DEPENDENCY="yes"
TEST_CHECK_KCONFIG="yes"
TEST_BUILD_ALLYES="yes"
TEST_BUILD_ALLNO="yes"
TEST_BUILD_DEFCONFIG="yes"
TEST_BUILD_DEBUG="yes"
TEST_RPM_BUILD="yes"
TEST_CHECK_KAPI="yes"
TEST_BOOT_KERNEL="yes"

# Host Configuration
HOST_USER_PWD="${params.Host_configuration}"

# VM Configuration
VM_IP="${params.VM_ip}"
VM_ROOT_PWD="${params.VM_root_pwd}"

# Repository Configuration
TORVALDS_REPO="${env.WORKSPACE}/patch-precheck-ci/.torvalds-linux"
EOF
        """
        
        // Verify file was created and has content
        validate_config_file(configFile, ['LINUX_SRC_PATH', 'BUILD_THREADS'])
        
        echo "✔ Anolis configuration created and validated successfully"
        
    } catch (Exception e) {
        error("❌ Anolis configuration failed: ${e.message}")
    }
}

def euler_general_configuration() {
    try {
        def configFile = "${env.WORKSPACE}/patch-precheck-ci/euler/.configure"
        
        echo "Creating Euler configuration file..."
        
        // Validate required parameters
        validate_required_params([
            'PATCH_DIR': params.PATCH_DIR,
            'BUILD_THREADS': params.BUILD_THREADS,
            'Patch_category': params.Patch_category
        ])
        
        // Check if directory exists
        def configDir = "${env.WORKSPACE}/patch-precheck-ci/euler"
        if (!fileExists(configDir)) {
            error("❌ Euler directory not found: ${configDir}")
        }
        
        // Check if file exists
        if (fileExists(configFile)) {
            echo "⚠ Configuration file already exists - will be overwritten"
        }
        
        // Create configuration file
        sh """
            cd "${configDir}"
            cat > .configure <<'EOF'
# General Configuration
LINUX_SRC_PATH="${params.PATCH_DIR}"
SIGNER_NAME="${params.SIGNED_OFF_NAME}"
SIGNER_EMAIL="${params.SIGNED_OFF_EMAIL}"
BUGZILLA_ID="${params.BUGZILLA_ID}"
PATCH_CATEGORY="${params.Patch_category}"
NUM_PATCHES="${params.NO_OF_PATCHES != 'none' ? params.NO_OF_PATCHES.toInteger() : 0}"

# Build Configuration
BUILD_THREADS="${params.BUILD_THREADS != 'none' ? params.BUILD_THREADS.toInteger() : 1}"

# Test Configuration    
RUN_TESTS="yes"
TEST_CHECK_DEPENDENCY="yes"
TEST_BUILD_ALLMOD="yes"
TEST_CHECK_PATCH="yes"
TEST_CHECK_FORMAT="yes"
TEST_RPM_BUILD="yes"    
TEST_BOOT_KERNEL="yes"

# Host Configuration
HOST_USER_PWD="${params.Host_configuration}"

# VM Configuration
VM_IP="${params.VM_ip}"
VM_ROOT_PWD="${params.VM_root_pwd}"

# Repository Configuration
TORVALDS_REPO="${env.WORKSPACE}/patch-precheck-ci/.torvalds-linux"
EOF
        """
        
        // Verify file was created and has content
        validate_config_file(configFile, ['LINUX_SRC_PATH', 'PATCH_CATEGORY', 'BUILD_THREADS'])
        
        echo "✔ Euler configuration created and validated successfully"
        
    } catch (Exception e) {
        error("❌ Euler configuration failed: ${e.message}")
    }
}

def validate_required_params(Map params) {
    try {
        def missingParams = []
        
        params.each { key, value ->
            if (!value || value == 'none' || value.trim() == '') {
                missingParams.add(key)
            }
        }
        
        if (missingParams.size() > 0) {
            error("❌ Missing required parameters: ${missingParams.join(', ')}")
        }
        
    } catch (Exception e) {
        error("❌ Parameter validation failed: ${e.message}")
    }
}

def validate_config_file(String filePath, List expectedKeys) {
    try {
        // Check file exists
        if (!fileExists(filePath)) {
            error("❌ Configuration file not found: ${filePath}")
        }
        
        // Check file is not empty
        def fileSize = sh(
            script: "stat -c%s ${filePath}",
            returnStdout: true
        ).trim().toInteger()
        
        if (fileSize == 0) {
            error("❌ Configuration file is empty: ${filePath}")
        }
        
        // Read file content
        def fileContent = sh(
            script: "cat ${filePath}",
            returnStdout: true
        ).trim()
        
        // Validate expected keys are present
        def missingKeys = []
        expectedKeys.each { key ->
            if (!fileContent.contains(key)) {
                missingKeys.add(key)
            }
        }
        
        if (missingKeys.size() > 0) {
            error("❌ Configuration file missing expected keys: ${missingKeys.join(', ')}")
        }
        
        echo "✔ Configuration file validated: ${filePath} (${fileSize} bytes)"
        
    } catch (Exception e) {
        error("❌ Configuration file validation failed: ${e.message}")
    }
}

def anolis_test_configuration() {
    try {
        echo "Configuring Anolis tests..."
        
        def cmd = ""
        def testName = params.Anolis_Selected_tests

        switch(testName) {
            case "all":
                cmd = "make test"
                break
	    case "check_dependency":
                cmd = "make anolis-test=check_dependency"
                break 
            case "check_Kconfig":
                cmd = "make anolis-test=check_kconfig"
                break
            case "build_allyes_config":
                cmd = "make anolis-test=build_allyes_config"
                break
            case "build_allno_config":
                cmd = "make anolis-test=build_allno_config"
                break
            case "build_anolis_defconfig":
                cmd = "make anolis-test=build_anolis_defconfig"
                break
            case "build_anolis_debug":
                cmd = "make anolis-test=build_anolis_debug"
                break
            case "anck_rpm_build":
                cmd = "make anolis-test=anck_rpm_build"
                break
            case "check_kapi":
                cmd = "make anolis-test=check_kapi"
                break
            case "boot_kernel_rpm":
                cmd = "make anolis-test=boot_kernel_rpm"
                break
            case "none":
                echo "⚠ No test selected - skipping test execution"
                return
            default:
                error("❌ Unknown test selection: ${testName}")
        }

        echo "Executing test command: ${cmd}"
        
        sh """
            set -e
            cd "${env.WORKSPACE}/patch-precheck-ci"
            ${cmd}
        """
        
        echo "✔ Test execution completed successfully"
        
    } catch (Exception e) {
        error("❌ Anolis test configuration failed: ${e.message}")
    }
}

def euler_test_configuration() {
    try {
        echo "Configuring Euler tests..."
        
        def cmd = ""
        def testName = params.Euler_Selected_tests

        switch(testName) {
            case "all":
                cmd = "make test"
                break
            case "check_dependency":
                cmd = "make euler-test=check_dependency"
                break
            case "build_allmod":
                cmd = "make euler-test=build_allmod"
                break
            case "check_patch":
                cmd = "make euler-test=check_patch"
                break
            case "check_format":
                cmd = "make euler-test=check_format"
                break
            case "rpm_build":
                cmd = "make euler-test=rpm_build"
                break
            case "boot_kernel":
                cmd = "make euler-test=boot_kernel"
                break
            case "none":
                echo "⚠ No test selected - skipping test execution"
                return
            default:
                error("❌ Unknown test selection: ${testName}")
        }

        echo "Executing test command: ${cmd}"
        
        sh """
            set -e
            cd "${env.WORKSPACE}/patch-precheck-ci"
            ${cmd}
        """
        
        echo "✔ Test execution completed successfully"
        
    } catch (Exception e) {
        error("❌ Euler test configuration failed: ${e.message}")
    }
}

void MyArchive() {
    try {
        echo "Starting artifact archival..."
        
        def result_dir = "${env.WORKSPACE}/../result_logs/${JOB_NAME}/${env.BUILD_NUMBER}"
        def logs_dir = "${env.WORKSPACE}/patch-precheck-ci/logs"
        
        echo "Result directory: ${result_dir}"
        echo "Logs directory: ${logs_dir}"
        
        // Create result directory
        sh """
            set -e
            mkdir -p ${result_dir}
        """
        
        // Verify result directory was created
        if (!fileExists(result_dir)) {
            error("❌ Failed to create result directory: ${result_dir}")
        }
        
        // Check if logs directory exists
        if (!fileExists(logs_dir)) {
            echo "⚠ Warning: Logs directory not found: ${logs_dir}"
            echo "Creating empty logs directory for archival..."
            sh """
                mkdir -p ${logs_dir}
                echo "No logs generated" > ${logs_dir}/README.txt
            """
        }
        
        // Copy logs to result directory
        sh """
            set -e
            cp -r ${logs_dir} ${result_dir}/
        """
        
        // Verify logs were copied
        def copiedLogs = "${result_dir}/logs"
        if (!fileExists(copiedLogs)) {
            error("❌ Failed to copy logs to result directory")
        }
        
        // Check if copied directory has content
        def logCount = sh(
            script: "find ${copiedLogs} -type f | wc -l",
            returnStdout: true
        ).trim().toInteger()
        
        echo "✔ Archived ${logCount} log file(s) to: ${result_dir}"
        
        // Archive artifacts using Jenkins
        archiveArtifacts artifacts: '**/logs/**/*', 
                        allowEmptyArchive: true,
                        fingerprint: true
        
        echo "✔ Artifact archival completed successfully"
        
    } catch (Exception e) {
        error("❌ Artifact archival failed: ${e.message}")
    }
} 
