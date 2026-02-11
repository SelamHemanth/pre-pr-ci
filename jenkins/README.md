# ⚡ Patch Pre-Check CI Tool ⚡

This tool automates distribution detection, configuration, patch application, and kernel build/test workflows across supported Linux distributions.

## 🚀 Getting Started

Follow the instructions below to set up and use the automation scripts.

## 🔧 Workflow

Copy and paste the ['jenkins_pipeline.groovy'](https://github.com/d-priyankamani/patch-precheck-ci/blob/master/jenkins/jenkins_pipeline.groovy) code into the Jenkins Groovy script console.

Click Apply and then Save.

Select Build with Parameters.

Configure the options and click Build.

## 📌 Important Note

The very first build must be triggered without parameters.

Once the initial build completes, Jenkins will automatically update and expose the parameter options.

From the second build onward, you can select and customize parameters as needed.

## 👤 Author

Name: Priyanka Mani

Email: DMani.Priyanka@amd.com
